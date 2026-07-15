#!/bin/bash
cd ~/brain-agent

cat << 'EOF' > docker-compose.yml
version: "3.9"
services:
  line-bot:
    build: .
    container_name: line-bot
    restart: always
    ports:
      - "8000:8000"
    env_file: .env
    depends_on:
      - redis
      - litellm
    volumes:
      - ./data:/app/data

  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    container_name: litellm
    restart: always
    ports:
      - "4000:4000"
    volumes:
      - ./litellm_config.yaml:/app/config.yaml
    command: ["--config", "/app/config.yaml"]
    env_file: .env

  redis:
    image: redis:7-alpine
    container_name: redis
    restart: always
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data

volumes:
  redis_data:
EOF

cat << 'EOF' > .env.example
LINE_CHANNEL_SECRET=your_channel_secret_here
LINE_CHANNEL_ACCESS_TOKEN=your_channel_access_token_here
ANTHROPIC_API_KEY=sk-ant-xxxxx
OPENAI_API_KEY=sk-xxxxx
LITELLM_MASTER_KEY=sk-litellm-your-secret-key
LITELLM_URL=http://litellm:4000
REDIS_URL=redis://redis:6379
EOF

cp .env.example .env

mkdir -p data/brain/{raw/{conversations,notes,clips},wiki/{knowledge,people,projects,decisions},schema,privacy,quarantine}

echo "Setup complete! Edit .env with your API keys."
