ARG KCP_BASE_IMAGE=python:3.12-slim
FROM ${KCP_BASE_IMAGE}
ARG KCP_SOURCE_SHA=unsealed
LABEL org.opencontainers.image.revision="${KCP_SOURCE_SHA}"
ENV KCP_BUILD_SOURCE_SHA=${KCP_SOURCE_SHA}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 KCP_CLOUD_MODE=shadow
WORKDIR /app
USER root
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && (id kcp >/dev/null 2>&1 || useradd --uid 10001 --create-home kcp)
COPY __init__.py protocol.py storage.py service.py connection.py kaggle_monitor.py ./cloud_orchestrator/
COPY transport.py cpu_fixture.py coordinator.py coordinator_service.py ./cloud_orchestrator/
COPY fm_tail.py fm_transport.py fm_service.py native_checkpoint.py continuation.py ./cloud_orchestrator/
USER 10001
CMD ["python", "-m", "cloud_orchestrator.service"]
