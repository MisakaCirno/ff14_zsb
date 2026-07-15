import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const TOKENS_PATH = fileURLToPath(
  new URL("../src/styles/tokens.css", import.meta.url),
);
const TEXT_MINIMUM = 4.5;
const UI_MINIMUM = 3;

function parseCustomProperties(source) {
  const withoutComments = source.replace(/\/\*[\s\S]*?\*\//g, "");
  const properties = new Map();
  const declarationPattern = /(--[a-zA-Z0-9_-]+)\s*:\s*([^;]+);/g;

  for (const match of withoutComments.matchAll(declarationPattern)) {
    properties.set(match[1], match[2].trim());
  }

  return properties;
}

const rawTokens = parseCustomProperties(readFileSync(TOKENS_PATH, "utf8"));

function splitVarArguments(value) {
  let depth = 0;

  for (let index = 0; index < value.length; index += 1) {
    const character = value[index];

    if (character === "(") {
      depth += 1;
    } else if (character === ")") {
      depth -= 1;
    } else if (character === "," && depth === 0) {
      return [value.slice(0, index).trim(), value.slice(index + 1).trim()];
    }
  }

  return [value.trim(), null];
}

function resolveVariables(value, ancestry) {
  let result = "";
  let cursor = 0;

  while (cursor < value.length) {
    const remaining = value.slice(cursor);
    const functionMatch = /var\s*\(/.exec(remaining);

    if (!functionMatch) {
      result += remaining;
      break;
    }

    const functionStart = cursor + functionMatch.index;
    const openParenthesis =
      functionStart + functionMatch[0].lastIndexOf("(");
    let depth = 1;
    let functionEnd = -1;

    for (
      let index = openParenthesis + 1;
      index < value.length;
      index += 1
    ) {
      if (value[index] === "(") {
        depth += 1;
      } else if (value[index] === ")") {
        depth -= 1;

        if (depth === 0) {
          functionEnd = index;
          break;
        }
      }
    }

    if (functionEnd === -1) {
      throw new Error(`unterminated var() in "${value}"`);
    }

    const [tokenName, fallback] = splitVarArguments(
      value.slice(openParenthesis + 1, functionEnd),
    );

    if (!/^--[a-zA-Z0-9_-]+$/.test(tokenName)) {
      throw new Error(`invalid custom property name "${tokenName}"`);
    }

    let replacement;

    if (rawTokens.has(tokenName)) {
      replacement = resolveToken(tokenName, ancestry);
    } else if (fallback !== null && fallback !== "") {
      replacement = resolveVariables(fallback, ancestry);
    } else {
      throw new Error(`missing token ${tokenName}`);
    }

    result += value.slice(cursor, functionStart) + replacement;
    cursor = functionEnd + 1;
  }

  return result.trim();
}

function resolveToken(tokenName, ancestry = []) {
  if (!rawTokens.has(tokenName)) {
    throw new Error(`missing token ${tokenName}`);
  }

  if (ancestry.includes(tokenName)) {
    throw new Error(
      `circular var() reference: ${[...ancestry, tokenName].join(" -> ")}`,
    );
  }

  return resolveVariables(rawTokens.get(tokenName), [...ancestry, tokenName]);
}

function parsePercentageOrNumber(value, scale) {
  const trimmed = value.trim();
  const match = /^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)(%)?$/i.exec(
    trimmed,
  );

  if (!match) {
    throw new Error(`invalid numeric channel "${value}"`);
  }

  const number = Number(match[1]);

  if (match[2]) {
    if (number < 0 || number > 100) {
      throw new Error(`percentage channel outside 0%-100%: "${value}"`);
    }

    return (number / 100) * scale;
  }

  if (number < 0 || number > scale) {
    throw new Error(`numeric channel outside 0-${scale}: "${value}"`);
  }

  return number;
}

function parseAlpha(value) {
  return parsePercentageOrNumber(value, 1);
}

function parseRgbFunction(value) {
  const match = /^rgba?\(\s*([\s\S]*?)\s*\)$/i.exec(value);

  if (!match) {
    return null;
  }

  const body = match[1];
  let channels;
  let alpha = 1;

  if (body.includes(",")) {
    const parts = body.split(",").map((part) => part.trim());

    if (parts.length !== 3 && parts.length !== 4) {
      throw new Error(`rgb()/rgba() needs 3 color channels: "${value}"`);
    }

    channels = parts.slice(0, 3);
    if (parts.length === 4) {
      alpha = parseAlpha(parts[3]);
    }
  } else {
    const slashParts = body.split("/").map((part) => part.trim());

    if (slashParts.length > 2) {
      throw new Error(`rgb() has more than one alpha separator: "${value}"`);
    }

    channels = slashParts[0].split(/\s+/).filter(Boolean);
    if (channels.length !== 3) {
      throw new Error(`rgb() needs 3 color channels: "${value}"`);
    }

    if (slashParts.length === 2) {
      alpha = parseAlpha(slashParts[1]);
    }
  }

  return {
    r: parsePercentageOrNumber(channels[0], 255),
    g: parsePercentageOrNumber(channels[1], 255),
    b: parsePercentageOrNumber(channels[2], 255),
    a: alpha,
  };
}

function parseHexColor(value) {
  const match = /^#([0-9a-f]{3,8})$/i.exec(value);

  if (!match || ![3, 4, 6, 8].includes(match[1].length)) {
    return null;
  }

  const digits =
    match[1].length <= 4
      ? [...match[1]].map((digit) => `${digit}${digit}`).join("")
      : match[1];

  return {
    r: Number.parseInt(digits.slice(0, 2), 16),
    g: Number.parseInt(digits.slice(2, 4), 16),
    b: Number.parseInt(digits.slice(4, 6), 16),
    a: digits.length === 8 ? Number.parseInt(digits.slice(6, 8), 16) / 255 : 1,
  };
}

function parseColor(value) {
  const resolved = value.trim();

  if (resolved.toLowerCase() === "transparent") {
    return { r: 0, g: 0, b: 0, a: 0 };
  }

  if (resolved.toLowerCase() === "black") {
    return { r: 0, g: 0, b: 0, a: 1 };
  }

  if (resolved.toLowerCase() === "white") {
    return { r: 255, g: 255, b: 255, a: 1 };
  }

  const color = parseHexColor(resolved) ?? parseRgbFunction(resolved);

  if (!color) {
    throw new Error(`unsupported color "${value}"`);
  }

  return color;
}

function tokenColor(tokenName) {
  return parseColor(resolveToken(tokenName));
}

function composite(foreground, background) {
  const alpha = foreground.a + background.a * (1 - foreground.a);

  if (alpha === 0) {
    return { r: 0, g: 0, b: 0, a: 0 };
  }

  return {
    r:
      (foreground.r * foreground.a +
        background.r * background.a * (1 - foreground.a)) /
      alpha,
    g:
      (foreground.g * foreground.a +
        background.g * background.a * (1 - foreground.a)) /
      alpha,
    b:
      (foreground.b * foreground.a +
        background.b * background.a * (1 - foreground.a)) /
      alpha,
    a: alpha,
  };
}

function opaque(color, backdrop, role) {
  const flattened = color.a === 1 ? color : composite(color, backdrop);

  if (Math.abs(flattened.a - 1) > Number.EPSILON) {
    throw new Error(`${role} is still transparent after compositing`);
  }

  return flattened;
}

function linearChannel(channel) {
  const normalized = channel / 255;
  return normalized <= 0.04045
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4;
}

function relativeLuminance(color) {
  return (
    0.2126 * linearChannel(color.r) +
    0.7152 * linearChannel(color.g) +
    0.0722 * linearChannel(color.b)
  );
}

function contrastRatio(first, second) {
  const lighter = Math.max(relativeLuminance(first), relativeLuminance(second));
  const darker = Math.min(relativeLuminance(first), relativeLuminance(second));
  return (lighter + 0.05) / (darker + 0.05);
}

function describeColor(color) {
  return `rgb(${Math.round(color.r)}, ${Math.round(color.g)}, ${Math.round(color.b)})`;
}

const contrastChecks = [];

function addText(label, foreground, background, backdrop) {
  contrastChecks.push({
    category: "text",
    label,
    foreground,
    background,
    backdrop,
    minimum: TEXT_MINIMUM,
  });
}

function addUi(label, foreground, background, backdrop) {
  contrastChecks.push({
    category: "ui",
    label,
    foreground,
    background,
    backdrop,
    minimum: UI_MINIMUM,
  });
}

for (const background of [
  "--app-color-canvas",
  "--app-color-surface",
  "--app-color-surface-muted",
]) {
  const backgroundLabel = background.replace("--app-color-", "");
  addText(
    `body text on ${backgroundLabel}`,
    "--app-color-text",
    background,
  );
  addText(
    `muted text on ${backgroundLabel}`,
    "--app-color-text-muted",
    background,
  );
}

for (const background of ["--app-color-canvas", "--app-color-surface"]) {
  const backgroundLabel = background.replace("--app-color-", "");
  addText(
    `primary link on ${backgroundLabel}`,
    "--app-color-primary",
    background,
  );
}

const statusFamilies = [
  { name: "primary", label: "--app-color-on-strong" },
  { name: "secondary", label: "--app-color-on-strong" },
  { name: "success", label: "--app-color-on-strong" },
  { name: "warning", label: "--app-color-on-bright" },
  { name: "danger", label: "--app-color-on-strong" },
  { name: "info", label: "--app-color-on-bright" },
];

for (const family of statusFamilies) {
  for (const state of ["normal", "hover", "active"]) {
    const stateSuffix = state === "normal" ? "" : `-${state}`;
    addText(
      `${family.name} button ${state}`,
      family.label,
      `--app-color-${family.name}${stateSuffix}`,
    );
  }

  const softLabel = ["warning", "info"].includes(family.name)
    ? `--app-color-${family.name}-text`
    : `--app-color-${family.name}`;
  addText(
    `${family.name} text on soft status surface`,
    softLabel,
    `--app-color-${family.name}-soft`,
    "--app-color-surface",
  );
  addText(
    `${family.name} status text on surface`,
    softLabel,
    "--app-color-surface",
  );
  addUi(
    `${family.name} outline on surface`,
    softLabel,
    "--app-color-surface",
  );
}

addText(
  "disabled text on disabled background",
  "--app-color-disabled-text",
  "--app-color-disabled-bg",
);
addUi(
  "disabled border on disabled background",
  "--app-color-disabled-border",
  "--app-color-disabled-bg",
);
addUi(
  "disabled border on surface",
  "--app-color-disabled-border",
  "--app-color-surface",
);
addUi(
  "Bootstrap valid border on surface",
  "--app-color-success",
  "--app-color-surface",
);
addUi(
  "Bootstrap invalid border on surface",
  "--app-color-danger",
  "--app-color-surface",
);

for (const background of [
  "--app-color-canvas",
  "--app-color-surface",
  "--app-color-surface-muted",
]) {
  const backgroundLabel = background.replace("--app-color-", "");
  addUi(
    `control border on ${backgroundLabel}`,
    "--app-color-control-border",
    background,
  );
}

addUi(
  "strong border on surface",
  "--app-color-border-strong",
  "--app-color-surface",
);
addUi(
  "focus border on surface",
  "--app-color-focus-border",
  "--app-color-surface",
);
addUi(
  "focus border on primary soft surface",
  "--app-color-focus-border",
  "--app-color-primary-soft",
  "--app-color-surface",
);
addUi(
  "image focus inner against outer ring",
  "--app-color-image-focus-inner",
  "--app-color-image-focus-outer",
);
addUi(
  "image focus outer ring on canvas",
  "--app-color-image-focus-outer",
  "--app-color-canvas",
);

for (const background of [
  "--app-color-canvas",
  "--app-color-surface",
  "--app-color-surface-muted",
  "--app-color-primary-soft",
]) {
  const backgroundLabel = background.replace("--app-color-", "");
  addUi(
    `focus ring on ${backgroundLabel}`,
    "--app-focus-ring-color",
    background,
    "--app-color-surface",
  );
}

for (const background of [
  "--app-color-shell",
  "--app-color-shell-raised",
  "--app-color-shell-gradient-end",
]) {
  const backgroundLabel = background.replace("--app-color-", "");
  addText(
    `shell text on ${backgroundLabel}`,
    "--app-color-shell-text",
    background,
  );
  addText(
    `shell muted text on ${backgroundLabel}`,
    "--app-color-shell-text-muted",
    background,
  );
  addText(
    `danger text on ${backgroundLabel}`,
    "--app-color-danger-on-dark",
    background,
  );
  addUi(
    `shell control border on ${backgroundLabel}`,
    "--app-color-shell-control-border",
    background,
  );
  addUi(
    `shell focus ring on ${backgroundLabel}`,
    "--app-color-shell-focus-ring",
    background,
  );
  addUi(
    `shell notification on ${backgroundLabel}`,
    "--app-color-shell-notification",
    background,
  );
}

for (const overlay of ["--app-color-overlay", "--app-color-overlay-hover"]) {
  const overlayLabel = overlay.replace("--app-color-", "");
  addText(
    `overlay text on ${overlayLabel}`,
    "--app-color-canvas",
    overlay,
    "--app-color-surface",
  );
  addUi(
    `${overlayLabel} against underlying surface`,
    overlay,
    "--app-color-surface",
  );
}

const failures = [];
let passedContrastChecks = 0;

for (const check of contrastChecks) {
  try {
    const backdrop = tokenColor(check.backdrop ?? "--app-color-surface");
    const opaqueBackdrop = opaque(
      backdrop,
      { r: 255, g: 255, b: 255, a: 1 },
      `${check.label} backdrop`,
    );
    const background = opaque(
      tokenColor(check.background),
      opaqueBackdrop,
      `${check.label} background`,
    );
    const foreground = opaque(
      tokenColor(check.foreground),
      background,
      `${check.label} foreground`,
    );
    const ratio = contrastRatio(foreground, background);

    if (ratio + Number.EPSILON < check.minimum) {
      failures.push(
        `[${check.category}] ${check.label}: ${ratio.toFixed(2)}:1 < ${check.minimum.toFixed(2)}:1 ` +
          `(${describeColor(foreground)} on ${describeColor(background)})`,
      );
    } else {
      passedContrastChecks += 1;
    }
  } catch (error) {
    failures.push(`[${check.category}] ${check.label}: ${error.message}`);
  }
}

const requiredRgbBases = [
  "--app-color-primary",
  "--app-color-secondary",
  "--app-color-success",
  "--app-color-warning",
  "--app-color-danger",
  "--app-color-info",
];

function parseRgbTuple(value) {
  const resolved = value.trim();

  if (/^rgba?\(/i.test(resolved)) {
    const color = parseRgbFunction(resolved);
    if (color.a !== 1) {
      throw new Error("RGB parity token must not include transparency");
    }
    return [color.r, color.g, color.b];
  }

  if (resolved.includes("/")) {
    throw new Error("RGB parity token must contain exactly three channels");
  }

  const parts = resolved.includes(",")
    ? resolved.split(",").map((part) => part.trim())
    : resolved.split(/\s+/).filter(Boolean);

  if (parts.length !== 3) {
    throw new Error("RGB parity token must contain exactly three channels");
  }

  return parts.map((part) => parsePercentageOrNumber(part, 255));
}

const rgbTokenNames = new Set(
  [...rawTokens.keys()].filter((tokenName) => tokenName.endsWith("-rgb")),
);
for (const baseToken of requiredRgbBases) {
  rgbTokenNames.add(`${baseToken}-rgb`);
}

let passedRgbChecks = 0;

for (const rgbToken of [...rgbTokenNames].sort()) {
  const baseToken = rgbToken.slice(0, -4);

  try {
    const tuple = parseRgbTuple(resolveToken(rgbToken));
    const resolvedBase = resolveToken(baseToken);

    if (!/^#[0-9a-f]{3,8}$/i.test(resolvedBase)) {
      throw new Error(`${baseToken} must resolve to a hex color`);
    }

    const baseColor = parseColor(resolvedBase);

    if (baseColor.a !== 1) {
      throw new Error(`${baseToken} must be opaque`);
    }

    const baseTuple = [baseColor.r, baseColor.g, baseColor.b];
    const matches = tuple.every(
      (channel, index) => Math.abs(channel - baseTuple[index]) < 1e-9,
    );

    if (!matches) {
      failures.push(
        `[rgb] ${rgbToken} (${tuple.join(", ")}) does not match ` +
          `${baseToken} (${baseTuple.join(", ")})`,
      );
    } else {
      passedRgbChecks += 1;
    }
  } catch (error) {
    failures.push(`[rgb] ${rgbToken}: ${error.message}`);
  }
}

if (failures.length > 0) {
  console.error(`Color contrast check failed (${failures.length} issues):`);
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exitCode = 1;
} else {
  const textCount = contrastChecks.filter(
    (check) => check.category === "text",
  ).length;
  const uiCount = contrastChecks.length - textCount;
  console.log(
    "Color contrast check passed: " +
      `${passedContrastChecks} pairs (${textCount} text >= ${TEXT_MINIMUM.toFixed(1)}:1, ` +
      `${uiCount} UI >= ${UI_MINIMUM.toFixed(1)}:1) and ` +
      `${passedRgbChecks} RGB mappings verified.`,
  );
}
