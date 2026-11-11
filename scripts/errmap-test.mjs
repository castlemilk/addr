// Proves the error code connect-web v1 produces for an unreachable service
// (closed port): the CRM panel maps Unknown + Unavailable to its
// "service unreachable" hint. Uses the same ConnectError.from default path
// the transport takes (protocol/run-call.js).
import { Code, ConnectError } from "@connectrpc/connect";

try {
  await fetch("http://127.0.0.1:9/crm.v1.CrmService/WhoAmI", { method: "POST" });
  console.log("unexpected success");
  process.exit(1);
} catch (reason) {
  const err = ConnectError.from(reason);
  console.log("instanceof ConnectError:", err instanceof ConnectError);
  console.log(
    "code:",
    err.code,
    "Unknown=",
    Code.Unknown,
    "Unavailable=",
    Code.Unavailable
  );
  if (!(err instanceof ConnectError)) throw new Error("not a ConnectError");
  if (err.code !== Code.Unknown && err.code !== Code.Unavailable) {
    throw new Error(`unexpected code ${err.code}`);
  }
  console.log("ERRMAP OK");
}
