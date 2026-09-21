# Build Stage
FROM golang:1.22-alpine AS builder

WORKDIR /app

# Install git and build dependencies
RUN apk add --no-cache git ca-certificates

COPY go.mod go.sum* ./
RUN go mod download

COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-w -s" -o /app/bin/gateway ./services/gateway

# Production Stage
FROM alpine:3.19

RUN apk --no-cache add ca-certificates curl

WORKDIR /app

COPY --from=builder /app/bin/gateway /app/gateway

EXPOSE 8080
EXPOSE 2112

ENTRYPOINT ["/app/gateway"]
