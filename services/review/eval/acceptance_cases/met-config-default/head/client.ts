export function buildClient(config: ClientConfig): HttpClient {
  return new HttpClient({ timeoutMs: config.timeoutMs ?? 30_000 });
}
