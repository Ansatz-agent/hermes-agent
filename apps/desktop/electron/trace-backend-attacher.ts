export class TraceTransportUnavailableError extends Error {
  constructor(message = 'trace_transport_unavailable') {
    super(message)
    this.name = 'TraceTransportUnavailableError'
  }
}

type Descriptor<TChild> = { child: TChild; generation: string; root: string }

export async function attachTraceBackends<TChild>(
  descriptors: Array<Descriptor<TChild>>,
  write: (descriptor: Descriptor<TChild>) => Promise<void>,
  diagnose: (diagnostic: string) => void = () => {}
): Promise<{ attempted: number; failed: number; succeeded: number }> {
  const settled = await Promise.allSettled(descriptors.map(descriptor => write(descriptor)))
  let failed = 0

  settled.forEach((outcome, index) => {
    if (outcome.status === 'rejected') {
      failed += 1
      diagnose(`trace transport attach unavailable for generation ${descriptors[index].generation}`)
    }
  })

  return { attempted: descriptors.length, failed, succeeded: descriptors.length - failed }
}
