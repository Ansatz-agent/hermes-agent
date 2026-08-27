export type BridgeStatus = {
  state: 'checking' | 'authenticated' | 'signed_out' | 'locked'
  username: string | null
  account_id: string | null
  session_id: string | null
  installation_id: string | null
  principal_key: string | null
  predecessor_principal_key?: string | null
  runtime_instance_id: string
  epoch: number
  valid_until: number
  cloud_state: 'active' | 'unreachable' | 'reauth_required' | null
  validation_state: 'unknown' | 'validating' | 'online' | 'degraded'
  validation_reason: string | null
  last_validated_at: string | null
  legacy: boolean
  reason: string | null
}
