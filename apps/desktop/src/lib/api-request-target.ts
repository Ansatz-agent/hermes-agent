let connectionId: null | string = null

export function getApiRequestConnectionId(): null | string {
  return connectionId
}

export function setApiRequestConnectionId(value: null | string): void {
  connectionId = value?.trim() || null
}
