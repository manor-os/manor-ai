// Public surface of @manor/worker-sdk.
//
// Quickstart:
//
//   import { ManorWorker, NeedHumanInput } from "@manor/worker-sdk";
//
//   const worker = new ManorWorker({
//     endpoint: "https://manor.example.com",
//     workerId: "wkr_xxx",
//     secret: "wks_xxx",
//   });
//
//   worker.handle({ kind: "action", provider: "shopify" }, async (lease, ctx) => {
//     const action = lease.action_key!;
//     const creds = lease.credentials[0]?.value;
//     // ...do the work...
//     return { result: { order_id: "..." }, cost: { api_calls: 1, usd: 0 } };
//   });
//
//   await worker.runForever();

export { ManorClient } from "./client.js";
export type { ManorClientOptions } from "./client.js";
export { ManorWorker } from "./worker.js";
export type {
  HandlerReturn,
  LeaseContext,
  LeaseHandler,
  ManorWorkerOptions,
} from "./worker.js";
export {
  NeedHumanInput,
  NoHandlerError,
  WorkerClientError,
} from "./types.js";
export type {
  CredentialBundle,
  CredentialType,
  ExecutionMode,
  HeartbeatActiveLease,
  HeartbeatCapacity,
  HeartbeatCompletedLease,
  HeartbeatInstruction,
  HeartbeatRequest,
  HeartbeatResponse,
  Lease,
  LeaseCost,
  LeaseKind,
  LeaseResult,
  RiskLevel,
  WorkerState,
} from "./types.js";
