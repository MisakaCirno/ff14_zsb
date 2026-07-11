function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`
  const match = document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))

  return match ? decodeURIComponent(match.slice(prefix.length)) : null
}

export function getCsrfToken(): string | null {
  const metaToken = document
    .querySelector<HTMLMetaElement>('meta[name="csrf-token"]')
    ?.content.trim()

  return metaToken || readCookie('csrftoken')
}
