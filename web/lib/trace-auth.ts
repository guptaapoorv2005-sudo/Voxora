import { timingSafeEqual } from "node:crypto";

export function traceCredentialsValid(header: string | null): boolean {
  const secret = process.env.TRACE_ACCESS_SECRET;
  let supplied = "";
  try {
    if (header?.startsWith("Basic ")) {
      const decoded = Buffer.from(header.slice(6), "base64").toString("utf8");
      if (decoded.startsWith("trace:")) supplied = decoded.slice(6);
    }
  } catch { return false; }
  const left = Buffer.from(secret ?? "", "utf8");
  const right = Buffer.from(supplied, "utf8");
  return Boolean(secret && secret.length >= 32 && left.length === right.length && timingSafeEqual(left, right));
}
