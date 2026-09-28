# Segtrack

Cobertura da coleta em Aparecida a partir do mapa oficial e dos pulsos da Segtrack.

**Backend:** [Render](https://render.com) · https://github.com/alekal02/STBACK  
**Frontend:** [Vercel](https://vercel.com) · https://github.com/alekal02/STFfront

## Local

```bash
cd back
py -3 -m sagtrack servir
```

Painel: http://127.0.0.1:6480

As chaves ficam no `.env` desta máquina. Elas não entram no Git.

## Produção

O back sobe no Render com o `render.yaml`. O front sobe na Vercel com o `front/vercel.json`. Os dois repositórios publicam sozinhos a cada push.

WhatsApp de produção: `http://24.199.86.195:8080`

No painel do Render, preencha as variáveis marcadas para sync manual: Fulltrack, tokens da Wuzapi e, se o front não for `*.vercel.app`, o `CORS_ORIGIN`.

Para publicar as duas pontas a partir desta pasta:

```powershell
powershell -File publicar.ps1
```
