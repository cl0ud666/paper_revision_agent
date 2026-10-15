# Archived upstream: pin the source release; only use this service for local development.
FROM golang:1.24-alpine AS build
ENV CGO_ENABLED=0
RUN go install github.com/minio/minio@RELEASE.2025-10-15T17-29-55Z && \
    cp /go/pkg/mod/github.com/minio/minio@*/LICENSE /tmp/MINIO-LICENSE
FROM alpine:3.22
RUN apk add --no-cache ca-certificates && adduser -D -u 10001 minio && \
    mkdir -p /data && chown minio:minio /data
COPY --from=build /go/bin/minio /usr/local/bin/minio
COPY --from=build /tmp/MINIO-LICENSE /licenses/MINIO-AGPL-3.0.txt
USER minio
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
