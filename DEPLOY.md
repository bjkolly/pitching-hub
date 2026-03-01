# Deploy to Vercel

## Step 1 — Log in to Vercel (one-time)
```bash
vercel login
```
This opens a browser to authenticate with your Vercel account.

---

## Step 2 — Deploy
```bash
cd /Users/bjkolly/Claude/pitching-hub
vercel --prod --yes
```
When prompted:
- **Set up and deploy**: Y
- **Which scope**: your personal account
- **Link to existing project?**: N (first time) — name it `pitching-hub`
- **Directory**: `.` (current)
- **Override settings?**: N

---

## Step 3 — Set environment variables on Vercel
After deploy, run these in your terminal:

```bash
vercel env add JWT_SECRET production
# Paste this value: 36632d5109ea83d7d518223e029a39914664b609d2708a78195796ffac9d041f

vercel env add ADMIN_USER production
# Paste: admin

vercel env add ADMIN_PASS production
# Paste your chosen password (e.g. admin — change this!)
```

Or set them in the Vercel dashboard:
1. Go to https://vercel.com/dashboard → your project → Settings → Environment Variables
2. Add:
   | Name         | Value                                                              |
   |---|---|
   | JWT_SECRET   | 36632d5109ea83d7d518223e029a39914664b609d2708a78195796ffac9d041f |
   | ADMIN_USER   | admin                                                              |
   | ADMIN_PASS   | admin  ← **change this!**                                          |

---

## Step 4 — Redeploy with env vars applied
```bash
vercel --prod --yes
```

---

## Future deployments
Every `git push` to `main` will auto-deploy if you connect GitHub in the Vercel dashboard:
Settings → Git → Connect Git Repository → bjkolly/pitching-hub

---

## Default login credentials
| Field    | Value   |
|---|---|
| Username | `admin` |
| Password | `admin` |

⚠️  Change `ADMIN_PASS` in Vercel env vars before sharing the link publicly.

---

## GitHub repo
https://github.com/bjkolly/pitching-hub
