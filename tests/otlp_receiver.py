"""Receptores OTLP mínimos (gRPC e HTTP/protobuf) que guardam o que o exporter realmente enviou."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import grpc
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2, logs_service_pb2_grpc
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2, metrics_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc


def _value(any_value: Any) -> Any:
    kind = any_value.WhichOneof("value")
    return getattr(any_value, kind) if kind else None


def attrs(key_values: Any) -> dict[str, Any]:
    return {kv.key: _value(kv.value) for kv in key_values}


class Received:
    def __init__(self) -> None:
        self.traces: list[Any] = []
        self.metrics: list[Any] = []
        self.logs: list[Any] = []
        self._lock = threading.Lock()

    def add(self, kind: str, request: Any) -> None:
        with self._lock:
            getattr(self, kind).append(request)

    def spans(self) -> list[tuple[dict[str, Any], Any]]:
        return [
            (attrs(rs.resource.attributes), span)
            for req in self.traces
            for rs in req.resource_spans
            for ss in rs.scope_spans
            for span in ss.spans
        ]

    def log_records(self) -> list[tuple[dict[str, Any], Any]]:
        return [
            (attrs(rl.resource.attributes), rec)
            for req in self.logs
            for rl in req.resource_logs
            for sl in rl.scope_logs
            for rec in sl.log_records
        ]

    def metric_items(self) -> list[tuple[dict[str, Any], Any]]:
        return [
            (attrs(rm.resource.attributes), metric)
            for req in self.metrics
            for rm in req.resource_metrics
            for sm in rm.scope_metrics
            for metric in sm.metrics
        ]


class GrpcReceiver:
    def __init__(self) -> None:
        self.received = Received()
        received = self.received

        class Trace(trace_service_pb2_grpc.TraceServiceServicer):
            def Export(self, request, context):  # noqa: N802
                received.add("traces", request)
                return trace_service_pb2.ExportTraceServiceResponse()

        class Metrics(metrics_service_pb2_grpc.MetricsServiceServicer):
            def Export(self, request, context):  # noqa: N802
                received.add("metrics", request)
                return metrics_service_pb2.ExportMetricsServiceResponse()

        class Logs(logs_service_pb2_grpc.LogsServiceServicer):
            def Export(self, request, context):  # noqa: N802
                received.add("logs", request)
                return logs_service_pb2.ExportLogsServiceResponse()

        self._server = grpc.server(ThreadPoolExecutor(max_workers=4))
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Trace(), self._server)
        metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(Metrics(), self._server)
        logs_service_pb2_grpc.add_LogsServiceServicer_to_server(Logs(), self._server)
        self.port = self._server.add_insecure_port("127.0.0.1:0")

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop(grace=0)


class HttpReceiver:
    def __init__(self) -> None:
        self.received = Received()
        received = self.received
        routes = {
            "/v1/traces": (
                "traces",
                trace_service_pb2.ExportTraceServiceRequest,
                trace_service_pb2.ExportTraceServiceResponse,
            ),
            "/v1/metrics": (
                "metrics",
                metrics_service_pb2.ExportMetricsServiceRequest,
                metrics_service_pb2.ExportMetricsServiceResponse,
            ),
            "/v1/logs": (
                "logs",
                logs_service_pb2.ExportLogsServiceRequest,
                logs_service_pb2.ExportLogsServiceResponse,
            ),
        }

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                route = routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                kind, request_cls, response_cls = route
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.add(kind, request_cls.FromString(body))
                payload = response_cls().SerializeToString()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
