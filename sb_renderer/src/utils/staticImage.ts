export function getBoardUrl(boardName: string): string {
  return new URL(`../assets/background/${boardName}.webp`, import.meta.url).href
}

export function getIconUrl(iconName: string): string {
  return new URL(`../assets/objects/${iconName}.webp`, import.meta.url).href
}
