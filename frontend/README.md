# Frontend placeholder

The repository did not contain a React/Vite frontend when the Kestra Docker
stack was added. This directory exists so `docker-compose.yml` can start a
`frontend` service without a broken build context.

Replace `dist/` with the production build output of the real React app, then
run:

```powershell
docker compose build frontend
docker compose up -d frontend
```

The container serves static files with nginx on `http://127.0.0.1:5173`.
