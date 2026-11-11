# syntax=docker/dockerfile:1
FROM --platform=$BUILDPLATFORM golang:1.27-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd/annotation-server ./cmd/annotation-server
COPY internal/annotation ./internal/annotation
COPY gen/go/annotation ./gen/go/annotation
ARG TARGETOS=linux
ARG TARGETARCH=amd64
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH \
    go build -trimpath -ldflags="-s -w" -o /out/annotation-server ./cmd/annotation-server

FROM gcr.io/distroless/static-debian12:nonroot
# A new image rollout is required for the existing singleton to receive the
# shared-workspace startup environment; a Worker-only config deploy is insufficient.
LABEL dev.fieldwork.runtime-contract="user-workspaces-v1"
COPY --from=build /out/annotation-server /annotation-server
ENV ANNOTATION_LISTEN=:8080
EXPOSE 8080
USER 65532:65532
ENTRYPOINT ["/annotation-server"]
