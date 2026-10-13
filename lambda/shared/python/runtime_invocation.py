"""Bound AgentCore Runtime transport and response reads by one monotonic deadline.

Runs invocation and size-limited response reads in a worker thread, reserving
time for the caller to release its routing admission. Callers provide a client
factory that accepts connect/read timeouts and an absolute monotonic deadline.
A timeout stops waiting but cannot cancel a remote turn; ambiguous failures
carry retain_admission so the route remains protected until reconciliation.
"""

import queue
import threading
import time


RELEASE_RESERVE_SECONDS = 6


class InvocationDeadlineExceeded(TimeoutError):
    def __init__(self, message, retain_admission):
        super().__init__(message)
        self.retain_admission = retain_admission


class InvocationTransportError(RuntimeError):
    retain_admission = True


def invoke_bounded(client_factory, parameters, deadline, max_response_bytes=500_000):
    io_deadline = deadline - RELEASE_RESERVE_SECONDS
    budget = io_deadline - time.monotonic()
    if budget <= 0:
        raise InvocationDeadlineExceeded("No AgentCore Runtime I/O budget remains", retain_admission=False)
    results = queue.Queue(maxsize=1)

    def run():
        client, body = None, None
        try:
            remaining = io_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("AgentCore Runtime deadline elapsed before connection")
            connect_timeout = min(5, remaining / 4)
            client = client_factory(connect_timeout=connect_timeout, read_timeout=remaining - connect_timeout)
            response = client.invoke_agent_runtime(**parameters)
            body = response.get("response")
            chunks, total = [], 0
            if body and hasattr(body, "read"):
                raw_stream = getattr(body, "_raw_stream", None)
                while True:
                    remaining = io_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("AgentCore Runtime response deadline elapsed")
                    # EOF closes urllib3's socket; still read to validate Content-Length.
                    if hasattr(body, "set_socket_timeout") and not getattr(raw_stream, "closed", False):
                        body.set_socket_timeout(remaining)
                    chunk = body.read(min(65536, max_response_bytes + 1 - total))
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode()
                    total += len(chunk)
                    if total > max_response_bytes:
                        raise ValueError("AgentCore Runtime response exceeds size limit")
                    chunks.append(chunk)
                content = b"".join(chunks)
            else:
                content = body if isinstance(body, bytes) else str(body or "").encode()
                if len(content) > max_response_bytes:
                    raise ValueError("AgentCore Runtime response exceeds size limit")
            results.put((True, (response.get("statusCode", 200), content)))
        except Exception as error:
            results.put((False, error))
        finally:
            for resource in (body, client):
                if resource and hasattr(resource, "close"):
                    try:
                        resource.close()
                    except Exception:
                        pass

    # A deadline ends waiting, not the accepted remote turn. Its admission stays.
    threading.Thread(target=run, daemon=True).start()
    try:
        ok, result = results.get(timeout=max(0, io_deadline - time.monotonic()))
    except queue.Empty as error:
        raise InvocationDeadlineExceeded("AgentCore Runtime transport or response deadline exceeded", True) from error
    if time.monotonic() >= io_deadline:
        raise InvocationDeadlineExceeded("AgentCore Runtime response arrived after the deadline", True)
    if not ok:
        raise InvocationTransportError(str(result)) from result
    return result
