# Networkless Linux compute job image — INACTIVE NON-PRODUCTION SHAPE ONLY.
#
# The real Docker build, ARM64 image, and static-scan gates are OPEN and are
# NOT run in this local, offline change. This Dockerfile documents the reviewed
# image contract a future reviewed implementation would have to satisfy:
#   - pinned-by-digest minimal base (no mutable tag)
#   - non-root user
#   - read-only root filesystem at runtime (enforced by the task definition)
#   - NO package installer, NO credentials, NO network route, NO live mount
#   - ARM64 only
#   - the reviewed compute.runner reference entrypoint; same-interpreter fences
#     are defense in depth and do not establish production isolation
#
# The base digest below is an illustrative placeholder. A future reviewed
# compute release would have to replace it before any build; the active release
# pipeline does not build this image.

# hadolint ignore=DL3006
FROM --platform=linux/arm64 gcr.io/distroless/python3-debian12@sha256:0000000000000000000000000000000000000000000000000000000000000000 AS runtime

# The distroless base ships no shell and no package manager by construction.
# Only the local compute package and the capability package it imports are
# copied into the inactive reference shape. PYTHONPATH points directly at that
# package root so ``python -m compute.runner`` resolves exactly once.
COPY lambda/compute /app/lambda/compute
COPY lambda/capabilities /app/lambda/capabilities
ENV PYTHONPATH=/app/lambda

# Non-root, unprivileged runtime identity. The synthesized task definition
# additionally enforces a read-only root filesystem and drops all Linux
# capabilities. Live exact-task evidence remains OPEN.
USER 10001:10001

WORKDIR /app

# The immutable input directory and the fresh output directory are provided by
# the isolated task at runtime; the image mounts neither a live workspace nor
# any AWS credential source.
ENTRYPOINT ["python", "-m", "compute.runner"]
CMD ["/job/input", "/job/output"]
