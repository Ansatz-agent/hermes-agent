export type TraceBackendDescriptor<TChild> = {
  child: TChild
  generation: string
  root: string
}

export class TraceBackendRegistry<TChild extends object> {
  private readonly entries = new Map<TChild, Omit<TraceBackendDescriptor<TChild>, 'child'>>()

  register(child: TChild, descriptor: { generation: string; root: string | null | undefined }): void {
    if (!descriptor.root) {
      throw new Error('trace_backend_root_unavailable')
    }

    this.entries.set(child, { generation: descriptor.generation, root: descriptor.root })
  }

  unregister(child: TChild, generation: string): boolean {
    const current = this.entries.get(child)

    if (!current || current.generation !== generation) {
      return false
    }

    return this.entries.delete(child)
  }

  active(): Array<TraceBackendDescriptor<TChild>> {
    return [...this.entries].map(([child, descriptor]) => ({ child, ...descriptor }))
  }

  clear(): void {
    this.entries.clear()
  }
}
