# Networkless Linux compute job image — SHAPE ONLY.
#
# The real Docker build, ARM64 image, and static-scan gates are OPEN and are
# NOT run in this local, offline change. This Dockerfile documents the reviewed
# image contract the external build must satisfy:
#   - pinned-by-digest minimal base (no mutable tag)
#   - non-root user
#   - read-only root filesystem at runtime (enforced by the task definition)
#   - NO package installer, NO credentials, NO network route, NO live mount
#   - ARM64 only
#   - the reviewed compute.runner entrypoint that fences all egress
#
# The base digest below is an illustrative placeholder; the release pipeline
# replaces it with the exact reviewed distroless digest before any build.

# hadolint ignore=DL3006
FROM --platform=linux/arm64 gcr.io/distroless/python3-debian12@sha256:0000000000000000000000000000000000000000000000000000000000000000 AS runtime

# The distroless base ships no shell and no package manager by construction.
# Only the reviewed, dependency-free compute package is copied in.
COPY lambda/compute /app/lambda/compute
COPY compute/runner.py /app/compute/runner.py

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
