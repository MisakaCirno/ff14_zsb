import Alpine from 'alpinejs'
import htmx from 'htmx.org'

import { getCsrfToken } from './core/csrf'
import { initializeAccountForms } from './features/account-forms'
import { initializeAnnouncement } from './features/announcement'
import { initializeFormControls } from './features/form-controls'
import { initializeInfiniteScrollStatus } from './features/infinite-scroll'
import { initializeManualCopy } from './features/manual-copy'
import { initializePreviewImages } from './features/preview-images'
import { initializeShareActions } from './features/share-actions'
import { initializeShareDetails } from './features/share-detail'
import { initializeShareEditors } from './features/share-editor'
import { initializeShareImages } from './features/share-image'
import { initializeShareInteractions } from './features/share-interactions'
import { initializeVisitHistory } from './features/visit-history'
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

document.addEventListener('htmx:configRequest', (event) => {
  const csrfToken = getCsrfToken()
  if (!csrfToken) {
    return
  }

  const detail = (event as CustomEvent<HtmxConfigRequestDetail>).detail
  detail.headers['X-CSRFToken'] = csrfToken
})

window.Alpine = Alpine
window.htmx = htmx
htmx.config.allowEval = false

initializeFormControls()
initializeAccountForms()
initializePreviewImages()
initializeVisitHistory()
initializeShareActions()
initializeAnnouncement()
initializeInfiniteScrollStatus()
initializeManualCopy()
initializeShareEditors()
initializeShareDetails()
initializeShareImages()
initializeShareInteractions()
Alpine.start()
