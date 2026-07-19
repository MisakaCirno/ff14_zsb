import { readFileSync, readdirSync } from "node:fs";
import { extname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PROJECT_DIRECTORY = fileURLToPath(new URL("../../", import.meta.url));
const STYLES_DIRECTORY = fileURLToPath(
  new URL("../src/styles/", import.meta.url),
);
const TEMPLATES_DIRECTORY = join(PROJECT_DIRECTORY, "templates");
const TOKENS_PATH = join(STYLES_DIRECTORY, "tokens.css");
const QUILL_ADAPTER_PATH = join(
  PROJECT_DIRECTORY,
  "static",
  "css",
  "quill-widget.css",
);
const SHARE_IMAGE_PATH = join(
  PROJECT_DIRECTORY,
  "frontend",
  "src",
  "features",
  "share-image.ts",
);
const failures = [];

const requiredTokens = new Map([
  ["--app-radius-control", "0.5rem"],
  ["--app-radius-surface", "0.75rem"],
  ["--app-radius-circle", "50%"],
]);

const tokenSource = readFileSync(TOKENS_PATH, "utf8");
const tokenPattern = /(--[a-zA-Z0-9_-]+)\s*:\s*([^;]+);/g;
const tokens = new Map(
  [...tokenSource.matchAll(tokenPattern)].map((match) => [
    match[1],
    match[2].trim(),
  ]),
);

for (const [name, expectedValue] of requiredTokens) {
  const actualValue = tokens.get(name);

  if (actualValue !== expectedValue) {
    failures.push(
      `tokens.css: ${name} must be ${expectedValue}, received ${actualValue ?? "missing"}`,
    );
  }
}

const obsoleteTokens = [
  "--app-radius-sm",
  "--app-radius-md",
  "--app-radius-lg",
  "--app-radius-xl",
  "--app-radius-pill",
  "--app-shadow-sm",
  "--app-shadow-md",
  "--app-shadow-hover",
  "--app-shadow-modal",
  "--app-shadow-lg",
  "--app-gradient-preview",
  "--app-gradient-shell",
];

for (const obsoleteToken of obsoleteTokens) {
  if (tokenSource.includes(obsoleteToken)) {
    failures.push(`tokens.css: obsolete token remains: ${obsoleteToken}`);
  }
}

const styleFiles = readdirSync(STYLES_DIRECTORY)
  .filter((fileName) => extname(fileName) === ".css")
  .sort();

let checkedRadiusDeclarations = 0;

for (const fileName of styleFiles) {
  if (fileName === "tokens.css") {
    continue;
  }

  const source = readFileSync(join(STYLES_DIRECTORY, fileName), "utf8");
  const lines = source.split(/\r?\n/);

  for (const [index, line] of lines.entries()) {
    const location = `${fileName}:${index + 1}`;

    if (/#[0-9a-f]{3,8}\b|rgba?\s*\(/i.test(line)) {
      failures.push(`${location}: raw colors belong in tokens.css`);
    }

    if (/\b(?:linear|radial|conic)-gradient\s*\(/i.test(line)) {
      failures.push(`${location}: gradients are outside the visual contract`);
    }

    for (const obsoleteToken of obsoleteTokens) {
      if (line.includes(obsoleteToken)) {
        failures.push(`${location}: use a semantic token instead of ${obsoleteToken}`);
      }
    }

    const radiusMatch = /border-radius\s*:\s*([^;]+);/.exec(line);

    if (!radiusMatch) {
      continue;
    }

    checkedRadiusDeclarations += 1;
    const value = radiusMatch[1].replace(/\s*!important\s*$/, "").trim();
    if (value.includes("calc(")) {
      failures.push(
        `${location}: derived radius values are not allowed: ${value}`,
      );
    }
    const withoutAllowedTokens = value
      .replace(
        /var\(--app-radius-(?:control|surface|circle)\)/g,
        "",
      )
      .replace(/\b0\b/g, "")
      .replace(/\bcalc\s*\(/g, "")
      .replace(/var\(--[a-zA-Z0-9_-]+\)/g, "")
      .replace(/[+*/().\s-]/g, "");

    if (value !== "0" && /(?:\d|%|px|rem|em)/i.test(withoutAllowedTokens)) {
      failures.push(`${location}: raw radius value is not allowed: ${value}`);
    }
  }
}

const collectFiles = (directory, extension) =>
  readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);

    if (entry.isDirectory()) {
      return collectFiles(path, extension);
    }

    return extname(entry.name) === extension ? [path] : [];
  });

const templateFiles = collectFiles(TEMPLATES_DIRECTORY, ".html");
const allowedStructuralRadiusClasses = new Set([
  "rounded-start-0",
  "rounded-end-0",
]);

for (const templatePath of templateFiles) {
  const source = readFileSync(templatePath, "utf8");
  const relativePath = templatePath.slice(PROJECT_DIRECTORY.length + 1);
  const classPattern = /class\s*=\s*(["'])(.*?)\1/gs;

  for (const match of source.matchAll(classPattern)) {
    const radiusClasses = match[2].match(/\brounded(?:-[a-z]+)?(?:-\d+)?\b/g) ?? [];

    for (const radiusClass of radiusClasses) {
      if (!allowedStructuralRadiusClasses.has(radiusClass)) {
        failures.push(
          `${relativePath}: Bootstrap radius utility is outside the visual contract: ${radiusClass}`,
        );
      }
    }
  }
}

const quillAdapterSource = readFileSync(QUILL_ADAPTER_PATH, "utf8");
if (!quillAdapterSource.includes(
  "border-radius: var(--app-radius-control, 0.5rem);",
)) {
  failures.push(
    "static/css/quill-widget.css: Quill code blocks must use the 8px control radius",
  );
}

const shareImageSource = readFileSync(SHARE_IMAGE_PATH, "utf8");
if (!shareImageSource.includes("const cornerRadius = 8")) {
  failures.push(
    "frontend/src/features/share-image.ts: exported QR cards must use the 8px control radius",
  );
}

if (failures.length > 0) {
  console.error(`Design-system check failed (${failures.length} issues):`);
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exitCode = 1;
} else {
  console.log(
    `Design-system check passed: ${styleFiles.length} stylesheets and ` +
      `${checkedRadiusDeclarations} radius declarations across ` +
      `${templateFiles.length} templates plus editor/export integrations ` +
      "follow the shared contract.",
  );
}
