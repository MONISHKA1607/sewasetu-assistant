# Deploying on one Ubuntu VM

1. Create an Ubuntu 22.04/24.04 VM (2 vCPU, 4 GB RAM, 30 GB disk). Open inbound ports 22, 80, 443 only.
   Note its public IP, e.g. 34.12.56.78.
2. Install Docker:   curl -fsSL https://get.docker.com | sh
3. Copy the project folder to the VM (scp -r, or git clone your private repo).
4. Choose a hostname. With no domain, use <ip with dashes>.sslip.io  ->  34-12-56-78.sslip.io
   (it resolves to the IP and Caddy can get a real HTTPS certificate for it).
5. cp deploy/env.example .env   then edit: DOMAIN, PUBLIC_BASE_URL (https://...), ADMIN_PASSWORD,
   SECRET_KEY (generate: openssl rand -hex 32).
6. Start:   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
   The first start loads 100,000 rows (a few minutes). Watch: docker compose logs -f app
7. Verify from your own laptop (not the VM):
     curl -I https://<DOMAIN>/                       -> 200
     curl -i -X POST https://<DOMAIN>/mcp/citizen    -> 401 with a WWW-Authenticate header
     curl https://<DOMAIN>/.well-known/oauth-authorization-server
     curl https://<DOMAIN>/.well-known/oauth-protected-resource/mcp/citizen
8. NEVER run "docker compose down -v" (it deletes the database). Restart safely with
   docker compose -f docker-compose.yml -f docker-compose.prod.yml restart
