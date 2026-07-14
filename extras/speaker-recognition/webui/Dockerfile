FROM node:18-alpine

# Install curl for health checks
RUN apk add curl

WORKDIR /app

# Accept build arguments for environment variables
ARG VITE_ALLOWED_HOSTS
ENV VITE_ALLOWED_HOSTS=$VITE_ALLOWED_HOSTS

# Install dependencies
COPY package*.json ./
RUN npm ci

# Copy source code
COPY . .

# Copy and set executable permissions for healthcheck script
COPY healthcheck.sh /app/healthcheck.sh
RUN chmod +x /app/healthcheck.sh

# Note: Port is determined at runtime by REACT_UI_PORT env var
# Actual port binding is configured in docker-compose.yml

# Start development server (plain HTTP; Caddy fronts it with TLS when HTTPS is on)
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
