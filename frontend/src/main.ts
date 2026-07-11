import Alpine from 'alpinejs'
import htmx from 'htmx.org'

import { fallbackCopyTextToClipboard } from './core/clipboard'
import { getCsrfToken } from './core/csrf'
import { showMessage } from './core/notify'
import { initializeAnnouncement } from './features/announcement'
import { initializeInfiniteScrollStatus } from './features/infinite-scroll'
import { initializeShareActions } from './features/share-actions'
import { initializeVisitHistory, updateHistoryDropdown } from './features/visit-history'
import './styles/main.css'

declare global {
  interface Window {
    Alpine: typeof Alpine
    fallbackCopyTextToClipboard: typeof fallbackCopyTextToClipboard
    htmx: typeof htmx
    showMessage: typeof showMessage
    updateHistoryDropdown: typeof updateHistoryDropdown
  }
}

interface HtmxConfigRequestDetail {
  headers: Record<string, string>
}

document.addEventListener('htmx:configRequest', (event) => {
  const csrfToken = getCsrfToken()
  if (!csrfToken) {
    return
  }

  const detail = (event as CustomEvent<HtmxConfigRequestDetail>).detail
  detail.headers['X-CSRFToken'] = csrfToken
})

window.Alpine = Alpine
window.fallbackCopyTextToClipboard = fallbackCopyTextToClipboard
window.htmx = htmx
window.showMessage = showMessage
window.updateHistoryDropdown = updateHistoryDropdown
htmx.config.allowEval = false

initializeVisitHistory()
initializeShareActions()
initializeAnnouncement()
initializeInfiniteScrollStatus()
Alpine.start()
