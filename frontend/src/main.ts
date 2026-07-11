import Alpine from 'alpinejs'
import htmx from 'htmx.org'

import './styles/main.css'

declare global {
  interface Window {
    Alpine: typeof Alpine
    htmx: typeof htmx
  }
}

interface HtmxConfigRequestDetail {
  headers: Record<string, string>
}

function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`
  const match = document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))

  return match ? decodeURIComponent(match.slice(prefix.length)) : null
}

document.addEventListener('htmx:configRequest', (event) => {
  const csrfToken = readCookie('csrftoken')
  if (!csrfToken) {
    return
  }

  const detail = (event as CustomEvent<HtmxConfigRequestDetail>).detail
  detail.headers['X-CSRFToken'] = csrfToken
})

window.Alpine = Alpine
window.htmx = htmx
htmx.config.allowEval = false

Alpine.start()
