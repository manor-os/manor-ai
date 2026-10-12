import { Navigate, useParams, useSearchParams } from "react-router-dom";

// Keep old history links working through the canonical Automation surface.
export default function JobLogs() {
  const { jobId } = useParams<{ jobId: string }>();
  const [searchParams] = useSearchParams();
  const target = new URLSearchParams();
  if (jobId) target.set("job", jobId);
  const runId = searchParams.get("run");
  if (runId) target.set("run", runId);
  return <Navigate to={`/jobs?${target}`} replace />;
}
