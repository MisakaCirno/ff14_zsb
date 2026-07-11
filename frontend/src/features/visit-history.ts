interface VisitHistoryItem {
  id: string
  title: string
  timestamp: string | number | null
}

const historyStorageKey = 'visitHistory'

function readVisitHistory(): VisitHistoryItem[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(historyStorageKey) ?? '[]')
    if (!Array.isArray(parsed)) {
      return []
    }

    return parsed.flatMap((item): VisitHistoryItem[] => {
      if (
        typeof item !== 'object'
        || item === null
        || !('id' in item)
        || typeof item.id !== 'string'
        || item.id.trim().length === 0
      ) {
        return []
      }

      const title = 'title' in item && typeof item.title === 'string' && item.title
        ? item.title
        : '未命名分享'
      const timestamp = 'timestamp' in item
        && (typeof item.timestamp === 'string' || typeof item.timestamp === 'number')
        ? item.timestamp
        : null

      return [{ id: item.id, title, timestamp }]
    })
  } catch (error) {
    console.warn('Ignoring invalid visit history data.', error)
    return []
  }
}

function createHistoryItem(item: VisitHistoryItem): HTMLLIElement {
  const listItem = document.createElement('li')
  const link = document.createElement('a')
  const content = document.createElement('div')
  const title = document.createElement('span')
  const dateLabel = document.createElement('small')

  link.className = 'dropdown-item text-truncate'
  link.href = `/s/${encodeURIComponent(item.id)}/`
  link.title = item.title
  content.className = 'd-flex justify-content-between align-items-center'
  title.className = 'text-truncate me-2'
  title.style.maxWidth = '160px'
  title.textContent = item.title

  const date = new Date(item.timestamp ?? Number.NaN)
  dateLabel.className = 'text-muted'
  dateLabel.style.fontSize = '0.75rem'
  dateLabel.textContent = Number.isNaN(date.getTime())
    ? ''
    : `${date.getMonth() + 1}-${date.getDate()}`

  content.append(title, dateLabel)
  link.appendChild(content)
  listItem.appendChild(link)
  return listItem
}

function updateHistoryDropdown(): void {
  const container = document.getElementById('historyList')
  const header = container?.querySelector('.dropdown-header')?.parentElement
  const divider = container?.querySelector('.dropdown-divider')?.parentElement
  if (!container || !header || !divider) {
    return
  }

  let current = header.nextElementSibling
  while (current && current !== divider) {
    const next = current.nextElementSibling
    current.remove()
    current = next
  }

  const history = readVisitHistory()
  if (history.length === 0) {
    const listItem = document.createElement('li')
    const emptyState = document.createElement('span')
    emptyState.className = 'dropdown-item-text text-muted small text-center'
    emptyState.textContent = '暂无访问记录'
    listItem.appendChild(emptyState)
    container.insertBefore(listItem, divider)
    return
  }

  for (const item of history) {
    container.insertBefore(createHistoryItem(item), divider)
  }
}

export function recordVisitHistory(id: string, title: string): void {
  const normalizedId = id.trim()
  if (!normalizedId) {
    return
  }

  try {
    const history = readVisitHistory().filter((item) => item.id !== normalizedId)
    history.unshift({
      id: normalizedId,
      title: title || '未命名分享',
      timestamp: Date.now(),
    })
    localStorage.setItem(historyStorageKey, JSON.stringify(history.slice(0, 10)))
    updateHistoryDropdown()
  } catch (error) {
    console.warn('Unable to save visit history.', error)
  }
}

export function initializeVisitHistory(): void {
  updateHistoryDropdown()
  document.addEventListener('click', (event) => {
    const target = event.target instanceof Element
      ? event.target.closest<HTMLElement>('[data-clear-history]')
      : null
    if (!target) {
      return
    }

    event.preventDefault()
    if (window.confirm('确定要清空访问历史吗？')) {
      localStorage.removeItem(historyStorageKey)
      updateHistoryDropdown()
    }
  })
}
