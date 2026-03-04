import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import multer from 'multer';
import { parse } from 'csv-parse';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { spawn } from 'child_process';
import cookieParser from 'cookie-parser';
import jwt from 'jsonwebtoken';
import bcrypt from 'bcryptjs';
import { TrackmanClient, normalizePitches } from './trackman.js';
import { enrichPitchers } from './appm.js';
import { importTeam, searchSchools } from './scraper.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

const app        = express();
const PORT       = process.env.PORT || 3001;
const IS_VERCEL  = !!process.env.VERCEL;

// On Vercel the project root is read-only — use /tmp instead
const DATA_DIR   = IS_VERCEL
  ? '/tmp/pitching-hub'
  : path.join(__dirname, '../data');
const SESSION      = path.join(DATA_DIR, 'session.json');
const CREW_SESSION = path.join(DATA_DIR, 'crew_session.json');
// On Vercel the repo's data/ dir is read-only but still deployed
const CREW_SESSION_REPO = path.join(__dirname, '../data/crew_session.json');
const TEAMS_DIR      = path.join(__dirname, '../data/teams');
const TEAMS_MANIFEST = path.join(TEAMS_DIR, 'manifest.json');
const SCHOOL_REGISTRY = path.join(__dirname, '../data/school_registry.json');
// Runtime teams dir: writable on both local and Vercel (/tmp)
const RUNTIME_TEAMS_DIR = IS_VERCEL
  ? path.join(DATA_DIR, 'teams')
  : path.join(__dirname, '../data/teams');
const RUNTIME_MANIFEST  = path.join(RUNTIME_TEAMS_DIR, 'manifest.json');
const AGENTS_DIR   = path.join(__dirname, '../agents');
const CLIENT_DIR   = path.join(__dirname, '../client');

// Ensure data directory exists (critical for Vercel /tmp path)
if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

// ── Auth config ───────────────────────────────────────────────────────────────
const JWT_SECRET      = process.env.JWT_SECRET   || 'pitching-hub-jwt-secret-change-me';
const ADMIN_USER      = process.env.ADMIN_USER   || 'admin';
const ADMIN_PASS      = process.env.ADMIN_PASS   || 'admin';
// Hash password once at startup (~100 ms) — avoids storing plaintext hash in env
const ADMIN_PASS_HASH = bcrypt.hashSync(ADMIN_PASS, 10);

// ── User store (multi-user support) ──────────────────────────────────────────
const USERS_FILE    = path.join(__dirname, '../data/users.json');
const RUNTIME_USERS = IS_VERCEL ? path.join(DATA_DIR, 'users.json') : USERS_FILE;

function readUsers() {
  const files = RUNTIME_USERS !== USERS_FILE
    ? [RUNTIME_USERS, USERS_FILE]
    : [USERS_FILE];
  for (const f of files) {
    if (fs.existsSync(f)) {
      try { return JSON.parse(fs.readFileSync(f, 'utf8')); }
      catch { /* continue */ }
    }
  }
  return [];
}

function writeUsers(users) {
  fs.writeFileSync(RUNTIME_USERS, JSON.stringify(users, null, 2));
  if (!IS_VERCEL && RUNTIME_USERS !== USERS_FILE) {
    fs.writeFileSync(USERS_FILE, JSON.stringify(users, null, 2));
  }
}

// ── Middleware ─────────────────────────────────────────────────────────────────
app.use(cors({ origin: true, credentials: true }));
app.use(express.json());
app.use(cookieParser());

// ── Multer (CSV uploads) ──────────────────────────────────────────────────────
const storage = multer.diskStorage({
  destination: (_req, _file, cb) => cb(null, DATA_DIR),
  filename:    (_req,  file, cb) => cb(null, `${Date.now()}-${file.originalname}`),
});
const upload = multer({
  storage,
  fileFilter: (_req, file, cb) => {
    if (!file.originalname.match(/\.csv$/i))
      return cb(new Error('Only CSV files are allowed'));
    cb(null, true);
  },
});

// ── Mock data generator ───────────────────────────────────────────────────────
const _r = (a, b) => Math.random() * (b - a) + a;
const _c = (v, a, b) => Math.max(a, Math.min(b, v));

const MOCK_NAMES = ['J. Rodriguez','M. Thompson','A. Williams','C. Martinez','D. Johnson',
                    'R. Garcia','T. Anderson','B. Wilson','K. Davis','L. Miller',
                    'P. Moore','S. Taylor','N. Jackson','E. White','F. Harris'];
const MOCK_ROLES = ['Ace','Ace','#2 SP','#2 SP','#3 SP','#3 SP','#4 SP','#4 SP',
                    '#5 SP','Long RP','Setup','Setup','Closer','Closer','Swingman'];
const MOCK_HANDS = ['RHP','LHP','RHP','RHP','LHP','RHP','LHP','RHP',
                    'RHP','RHP','LHP','RHP','RHP','LHP','RHP'];
const MOCK_PT    = [
  { n:'4-Seam FB', c:'#4a9eff' }, { n:'Curveball', c:'#a476ff' },
  { n:'Slider',    c:'#ff7c40' }, { n:'Changeup',  c:'#3dd68c' },
  { n:'Cutter',    c:'#ffe044' }, { n:'Sinker',    c:'#f04e5e' },
];

function _calcScore(m) {
  const n = (v, a, b) => _c((v - a) / (b - a), 0, 1);
  return Math.round(_c(
    n(m.stuffPlus, 60, 140) * 22 + n(m.cswPct, 17, 42) * 18 + n(m.whiffPct, 11, 40) * 16 +
    n(m.tunnelingScore, 28, 99) * 10 + (n(m.kPct, 11, 38) - n(m.bbPct, 2.5, 16)) * 0.5 * 12 +
    (1 - n(m.hardHitPct, 20, 52)) * 7 + n(m.seqScore, 28, 98) * 6 + n(m.chasePct, 20, 50) * 5 +
    n(m.avgVelo, 84, 101) * 2 + n(m.avgSpin, 1850, 2900) * 2,
    18, 99
  ));
}
function _genM(idx) {
  const q = idx === 0 ? 1.22 : idx < 3 ? 1.08 : idx < 7 ? 0.95 : 0.82;
  const m = {
    stuffPlus:      _c(Math.round(_r(70, 105) * q), 55, 148),
    cswPct:         _c(_r(20, 33) * (q * 0.85 + 0.15), 17, 42),
    whiffPct:       _c(_r(16, 30) * q, 11, 40),
    kPct:           _c(_r(17, 31) * q, 11, 38),
    bbPct:          _c(_r(4, 12) / q, 2.5, 16),
    hardHitPct:     _c(_r(27, 46) / q, 20, 52),
    tunnelingScore: _c(Math.round(_r(50, 80) * q), 28, 99),
    avgVelo:        _c(_r(88, 96) * (q * 0.55 + 0.45), 84, 101),
    avgSpin:        _c(Math.round(_r(2100, 2550) * (q * 0.55 + 0.45)), 1850, 2900),
    ivb:            _c(_r(11, 20) * q, 7, 25),
    hBreak:         _c(_r(8, 18), 4, 23),
    chasePct:       _c(_r(26, 40) * q, 20, 50),
    zonePct:        _c(_r(42, 58), 38, 62),
    seqScore:       _c(Math.round(_r(50, 80) * q), 28, 98),
  };
  m.score = _calcScore(m);
  return m;
}
function _genH(m, n = 10) {
  const h = []; let p = m.score;
  for (let i = 0; i < n; i++) {
    const s = _c(Math.round(p * 0.6 + m.score * 0.3 + _r(-10, 10) * 0.5), 18, 99);
    p = s; h.push(s);
  }
  return h;
}
function _genPD() {
  const cnt   = Math.floor(_r(3, 5));
  const types = MOCK_PT.slice(0, cnt).map(t => ({ ...t }));
  const pitches = [];
  types.forEach((t, ti) => {
    const num = Math.floor(_r(16, 30));
    for (let i = 0; i < num; i++) {
      const iz = Math.random() < 0.54;
      const x  = iz ? _r(-.7, .7) : _r(-1.35, 1.35);
      const y  = iz ? _r(1.5, 3.5) : _r(0.6, 4.4);
      const rv = Math.random();
      pitches.push({
        x: _c(x, -1.4, 1.4), y: _c(y, 0.5, 4.5),
        ti, c: t.c, res: rv < .32 ? 'w' : rv < .58 ? 'c' : 'h',
      });
    }
  });
  return { pitches, types };
}
function generateMockPitchers() {
  const PS = MOCK_NAMES.map((name, i) => {
    const m = _genM(i), h = _genH(m);
    return { id: i + 1, name, role: MOCK_ROLES[i], hand: MOCK_HANDS[i],
             metrics: m, history: h, pitchData: _genPD() };
  });
  // Apply APPM to mock data so it has confidence bands + form status
  const { pitchers } = enrichPitchers(PS);
  return pitchers;
}

// ── Helper: read session.json safely (falls back to crew_session.json) ───────
function readSession() {
  // Primary: user-uploaded / runtime session
  if (fs.existsSync(SESSION)) {
    try { return JSON.parse(fs.readFileSync(SESSION, 'utf8')); }
    catch (e) { console.error('[session] parse error:', e.message); }
  }
  // Fallback: crew-generated scouting data (/tmp on Vercel, data/ locally)
  for (const p of [CREW_SESSION, CREW_SESSION_REPO]) {
    if (fs.existsSync(p)) {
      try { return JSON.parse(fs.readFileSync(p, 'utf8')); }
      catch (e) { console.error(`[crew_session] parse error (${p}):`, e.message); }
    }
  }
  return null;
}

// ── Helper: enrich + persist session ─────────────────────────────────────────
function enrichAndSave(pitchers) {
  const { pitchers: enriched, datasetMean } = enrichPitchers(pitchers);
  fs.writeFileSync(SESSION, JSON.stringify(enriched, null, 2));
  return { enriched, datasetMean };
}

// ── Auth middleware ────────────────────────────────────────────────────────────
function requireAuth(req, res, next) {
  const token = req.cookies?.auth_token;
  if (!token) {
    if (req.originalUrl.startsWith('/api/') || req.xhr) return res.status(401).json({ error: 'Unauthorized' });
    return res.redirect('/login');
  }
  try {
    req.user = jwt.verify(token, JWT_SECRET);
    next();
  } catch {
    res.clearCookie('auth_token');
    if (req.originalUrl.startsWith('/api/') || req.xhr) return res.status(401).json({ error: 'Session expired' });
    res.redirect('/login');
  }
}

function requireAdmin(req, res, next) {
  // Fallback: treat env-var admin user as admin even with old JWT tokens
  if (!req.user?.isAdmin && req.user?.user !== ADMIN_USER) {
    return res.status(403).json({ error: 'Admin access required' });
  }
  next();
}

// ── Public routes (no auth required) ──────────────────────────────────────────

// Health check
app.get('/api/health', (_req, res) => {
  const client = new TrackmanClient();
  res.json({
    status:    'ok',
    timestamp: new Date().toISOString(),
    trackman:  client.isConfigured ? 'configured' : 'unconfigured (mock mode)',
    session:   fs.existsSync(SESSION) ? 'present' : 'absent',
    runtime:   IS_VERCEL ? 'vercel' : 'local',
  });
});

// Login page
app.get('/login', (req, res) => {
  try {
    jwt.verify(req.cookies?.auth_token, JWT_SECRET);
    return res.redirect('/');   // already logged in
  } catch { /* not authed, serve login */ }
  res.sendFile(path.join(CLIENT_DIR, 'login.html'));
});

// Login submit (checks env-var admin first, then users.json)
app.post('/login', async (req, res) => {
  const { username = '', password = '' } = req.body || {};
  let isAdmin = false;
  let valid = false;

  // Check env-var admin
  if (username === ADMIN_USER && bcrypt.compareSync(password, ADMIN_PASS_HASH)) {
    valid = true;
    isAdmin = true;
  }

  // Check users.json
  if (!valid) {
    const users = readUsers();
    const user = users.find(u => u.username === username);
    if (user && bcrypt.compareSync(password, user.passwordHash)) {
      valid = true;
      isAdmin = false;
    }
  }

  if (!valid) {
    return res.status(401).json({ error: 'Invalid credentials' });
  }

  const token = jwt.sign({ user: username, isAdmin }, JWT_SECRET, { expiresIn: '7d' });
  res.cookie('auth_token', token, {
    httpOnly: true,
    secure:   IS_VERCEL || process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    maxAge:   7 * 24 * 60 * 60 * 1000,   // 7 days
  });
  res.json({ ok: true });
});

// Logout
app.post('/logout', (req, res) => {
  res.clearCookie('auth_token');
  res.json({ ok: true });
});

// ── Protected: main app ───────────────────────────────────────────────────────
app.get('/', requireAuth, (_req, res) => {
  res.sendFile(path.join(CLIENT_DIR, 'index.html'));
});

// ── Protected: all /api/* routes ─────────────────────────────────────────────
app.use('/api', requireAuth);

// Current user info
app.get('/api/me', (req, res) => {
  // Fallback: treat env-var admin user as admin even if JWT lacks isAdmin claim (old token)
  const isAdmin = !!req.user.isAdmin || req.user.user === ADMIN_USER;
  res.json({ user: req.user.user, isAdmin });
});

// ── Admin: User Management ──────────────────────────────────────────────────
app.get('/api/admin/users', requireAdmin, (_req, res) => {
  const users = readUsers().map(u => ({
    name: u.name, username: u.username, createdAt: u.createdAt,
  }));
  res.json(users);
});

app.post('/api/admin/users', requireAdmin, (req, res) => {
  const { name, username, password } = req.body || {};
  if (!name || !username || !password) {
    return res.status(400).json({ error: 'name, username, and password are required' });
  }
  if (username.length < 3 || username.length > 30) {
    return res.status(400).json({ error: 'Username must be 3-30 characters' });
  }
  if (password.length < 6) {
    return res.status(400).json({ error: 'Password must be at least 6 characters' });
  }
  if (username === ADMIN_USER) {
    return res.status(409).json({ error: 'Cannot create user with admin username' });
  }
  const users = readUsers();
  if (users.find(u => u.username === username)) {
    return res.status(409).json({ error: 'Username already exists' });
  }
  const passwordHash = bcrypt.hashSync(password, 10);
  users.push({ name, username, passwordHash, createdAt: new Date().toISOString() });
  writeUsers(users);
  res.json({ ok: true, username });
});

app.delete('/api/admin/users/:username', requireAdmin, (req, res) => {
  const { username } = req.params;
  if (username === ADMIN_USER) {
    return res.status(400).json({ error: 'Cannot delete the admin user' });
  }
  const users = readUsers();
  const idx = users.findIndex(u => u.username === username);
  if (idx === -1) return res.status(404).json({ error: 'User not found' });
  users.splice(idx, 1);
  writeUsers(users);
  res.json({ ok: true });
});

// ── Helper: read team manifest (merges repo + runtime dirs for Vercel) ───────
function readTeamManifest() {
  const teams = [];
  const seen = new Set();

  // Repo manifest (always present, read-only on Vercel)
  if (fs.existsSync(TEAMS_MANIFEST)) {
    try {
      for (const t of JSON.parse(fs.readFileSync(TEAMS_MANIFEST, 'utf8'))) {
        if (!seen.has(t.slug)) { teams.push(t); seen.add(t.slug); }
      }
    } catch { /* ignore */ }
  }

  // Runtime manifest (imports on Vercel go to /tmp)
  if (RUNTIME_MANIFEST !== TEAMS_MANIFEST && fs.existsSync(RUNTIME_MANIFEST)) {
    try {
      for (const t of JSON.parse(fs.readFileSync(RUNTIME_MANIFEST, 'utf8'))) {
        if (!seen.has(t.slug)) { teams.push(t); seen.add(t.slug); }
      }
    } catch { /* ignore */ }
  }

  return teams;
}

// ── Helper: read a team's pitcher data (checks runtime then repo) ────────────
function readTeamData(slug) {
  // Check runtime dir first (newly imported teams), then repo dir
  const dirs = RUNTIME_TEAMS_DIR !== TEAMS_DIR
    ? [RUNTIME_TEAMS_DIR, TEAMS_DIR]
    : [TEAMS_DIR];

  for (const dir of dirs) {
    const file = path.join(dir, `${slug}.json`);
    if (fs.existsSync(file)) {
      try { return JSON.parse(fs.readFileSync(file, 'utf8')); }
      catch { /* continue */ }
    }
  }
  return null;
}

// GET /api/teams — list available teams (Sample Data first, then alphabetical)
app.get('/api/teams', (_req, res) => {
  const manifest = readTeamManifest();
  const sorted = manifest.slice().sort((a, b) => a.name.localeCompare(b.name));
  const teams = [
    { name: 'Sample Data', slug: 'sample' },
    ...sorted,
  ];
  res.json(teams);
});

// GET /api/pitchers — serve pitcher data; ?team= selects source
app.get('/api/pitchers', (req, res) => {
  const team = req.query.team;

  // Explicit team selection
  if (team && team !== 'sample') {
    const data = readTeamData(team);
    if (!data) return res.status(404).json({ error: `Team "${team}" not found` });
    const { pitchers } = enrichPitchers(data);
    return res.json(pitchers);
  }

  // "sample" or no param: session → mock
  if (team === 'sample') return res.json(generateMockPitchers());

  // Default (no param): session.json → crew_session → mock (backward compat)
  const session = readSession();
  if (session) return res.json(session);
  res.json(generateMockPitchers());
});

// GET /api/live — fetch live data from Trackman; falls back gracefully
app.get('/api/live', async (_req, res) => {
  const client = new TrackmanClient();

  if (!client.isConfigured) {
    console.log('[live] TRACKMAN_API_KEY not set — returning mock/session data');
    const fallback = readSession() ?? generateMockPitchers();
    return res.json({ source: 'mock', pitchers: fallback });
  }

  try {
    const pitchers = await client.fetchLive();
    const { enriched } = enrichAndSave(pitchers);
    console.log(`[live] fetched ${enriched.length} pitchers from Trackman`);
    res.json({ source: 'live', pitchers: enriched });
  } catch (err) {
    console.error('[live] Trackman API error:', err.message);
    const fallback = readSession() ?? generateMockPitchers();
    res.status(502).json({ source: 'fallback', error: err.message, pitchers: fallback });
  }
});

// POST /api/upload — parse Trackman CSV, transform, persist
app.post('/api/upload', upload.single('csv'), (req, res) => {
  if (!req.file) return res.status(400).json({ error: 'No file uploaded' });

  const rows = [];
  fs.createReadStream(req.file.path)
    .pipe(parse({ columns: true, skip_empty_lines: true, trim: true }))
    .on('data', row => rows.push(row))
    .on('error', err => res.status(500).json({ error: err.message }))
    .on('end', () => {
      try {
        const pitchers = normalizePitches(rows);
        fs.writeFileSync(SESSION, JSON.stringify(pitchers, null, 2));
        res.json({ message: 'Parsed and saved', count: pitchers.length, filename: req.file.filename });
      } catch (err) {
        res.status(500).json({ error: 'Transform failed', details: err.message });
      }
    });
});

// POST /api/analyze — run APPM against session.json (pure JS, no Python)
app.post('/api/analyze', (_req, res) => {
  if (!fs.existsSync(SESSION))
    return res.status(400).json({ error: 'No session data. Upload a CSV or call /api/live first.' });

  try {
    const raw  = JSON.parse(fs.readFileSync(SESSION, 'utf8'));
    const { enriched, datasetMean } = enrichAndSave(raw);
    const n          = enriched.length;
    const avgScore   = Math.round(enriched.reduce((a, p) => a + p.metrics.score,    0) / n * 10) / 10;
    const avgStuff   = Math.round(enriched.reduce((a, p) => a + p.metrics.stuffPlus, 0) / n * 10) / 10;
    const hot        = enriched.filter(p => p.formStatus === 'hot').length;
    const cold       = enriched.filter(p => p.formStatus === 'cold').length;
    const flagged    = enriched.filter(p => p.riskFlag).length;
    res.json({ status: 'ok', pitchers: n, avg_score: avgScore, avg_stuff: avgStuff,
               dataset_mean: Math.round(datasetMean * 10) / 10, hot, cold, flagged });
  } catch (err) {
    res.status(500).json({ error: 'Analysis failed', details: err.message });
  }
});

// GET /api/files — list CSVs in DATA_DIR
app.get('/api/files', (_req, res) => {
  fs.readdir(DATA_DIR, (err, files) => {
    if (err) return res.status(500).json({ error: 'Could not read data directory' });
    res.json({ files: files.filter(f => f.endsWith('.csv')) });
  });
});

// POST /api/session/load/:filename — re-parse stored CSV and re-enrich
app.post('/api/session/load/:filename', async (req, res) => {
  const filePath = path.join(DATA_DIR, req.params.filename);
  if (!fs.existsSync(filePath))
    return res.status(404).json({ error: 'File not found' });

  const rows = [];
  fs.createReadStream(filePath)
    .pipe(parse({ columns: true, skip_empty_lines: true, trim: true }))
    .on('data', row => rows.push(row))
    .on('error', err => res.status(500).json({ error: err.message }))
    .on('end', () => {
      try {
        const pitchers = normalizePitches(rows);
        const { enriched } = enrichAndSave(pitchers);
        res.json({ message: 'Session loaded', count: enriched.length, filename: req.params.filename });
      } catch (err) {
        res.status(500).json({ error: 'Transform failed', details: err.message });
      }
    });
});

// GET /api/data/:filename — parse and return CSV rows (debug)
app.get('/api/data/:filename', (req, res) => {
  const filePath = path.join(DATA_DIR, req.params.filename);
  if (!fs.existsSync(filePath)) return res.status(404).json({ error: 'File not found' });
  const rows = [];
  fs.createReadStream(filePath)
    .pipe(parse({ columns: true, skip_empty_lines: true, trim: true }))
    .on('data', row => rows.push(row))
    .on('error', err => res.status(500).json({ error: err.message }))
    .on('end', () => res.json({ rows, count: rows.length }));
});

// ── POST /api/run-agents — spawn CrewAI pipeline, stream output via SSE ──────
let agentProcess = null;   // track singleton so we can't double-run

app.post('/api/run-agents', (req, res) => {
  if (agentProcess) {
    return res.status(409).json({ error: 'Agent pipeline already running' });
  }

  // Resolve the Python binary inside the agents venv
  const pythonBin = path.join(AGENTS_DIR, 'venv', 'bin', 'python3');
  const mainPy    = path.join(AGENTS_DIR, 'main.py');

  if (!fs.existsSync(pythonBin)) {
    return res.status(500).json({ error: 'Python venv not found. Run setup first.' });
  }
  if (!fs.existsSync(mainPy)) {
    return res.status(500).json({ error: 'agents/main.py not found.' });
  }

  // SSE headers
  res.writeHead(200, {
    'Content-Type':  'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection':    'keep-alive',
    'X-Accel-Buffering': 'no',      // disable nginx buffering if present
  });

  const send = (event, data) => {
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  };

  send('status', { agent: 'system', message: 'Starting agent pipeline…' });

  agentProcess = spawn(pythonBin, ['-u', mainPy], {
    cwd:   AGENTS_DIR,
    env:   { ...process.env, PYTHONUNBUFFERED: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  // ── Parse stdout for agent-phase detection ──
  const AGENT_PATTERNS = [
    { re: /Batter Analyst|scout_batters|opponent.*batter/i,      name: 'Opponent Batter Scout' },
    { re: /Pitcher Analyst|scout_pitchers|target.*pitcher/i,     name: 'Target Pitcher Scout' },
    { re: /Matchup Specialist|matchup.*align/i,                  name: 'Matchup Aligner' },
    { re: /Predictive.*Analytics|season.*predict|Performance/i,  name: 'Season Predictor' },
  ];
  let currentAgent = '';

  function processLine(line) {
    const text = line.toString().trim();
    if (!text) return;

    // Detect which agent is running
    for (const { re, name } of AGENT_PATTERNS) {
      if (re.test(text) && name !== currentAgent) {
        currentAgent = name;
        send('agent', { agent: currentAgent });
        break;
      }
    }

    send('log', { agent: currentAgent || 'system', message: text });
  }

  // Buffer partial lines from stdout / stderr
  let stdoutBuf = '';
  agentProcess.stdout.on('data', chunk => {
    stdoutBuf += chunk.toString();
    const lines = stdoutBuf.split('\n');
    stdoutBuf = lines.pop();          // keep incomplete last line
    lines.forEach(processLine);
  });

  let stderrBuf = '';
  agentProcess.stderr.on('data', chunk => {
    stderrBuf += chunk.toString();
    const lines = stderrBuf.split('\n');
    stderrBuf = lines.pop();
    lines.forEach(l => {
      const text = l.trim();
      if (!text) return;
      send('log', { agent: currentAgent || 'system', message: `[stderr] ${text}` });
    });
  });

  agentProcess.on('close', (code) => {
    // Flush any remaining partial lines
    if (stdoutBuf.trim()) processLine(stdoutBuf);
    if (stderrBuf.trim()) send('log', { agent: 'system', message: `[stderr] ${stderrBuf.trim()}` });

    if (code === 0 && fs.existsSync(CREW_SESSION)) {
      // Enrich the crew output through APPM just like CSV uploads
      try {
        const raw = JSON.parse(fs.readFileSync(CREW_SESSION, 'utf8'));
        const { enriched } = enrichPitchers(raw);
        fs.writeFileSync(CREW_SESSION, JSON.stringify(enriched, null, 2));
        send('done', { success: true, pitchers: enriched.length });
      } catch (err) {
        send('done', { success: true, pitchers: 0, warning: `Enrich failed: ${err.message}` });
      }
    } else {
      send('done', { success: false, code, error: `Process exited with code ${code}` });
    }

    agentProcess = null;
    res.end();
  });

  agentProcess.on('error', (err) => {
    send('error', { message: err.message });
    agentProcess = null;
    res.end();
  });

  // If the client disconnects, kill the process
  req.on('close', () => {
    if (agentProcess) {
      agentProcess.kill('SIGTERM');
      agentProcess = null;
    }
  });
});

// ── School Import Endpoints ──────────────────────────────────────────────────

// GET /api/schools/search?q= — fuzzy search against school registry
app.get('/api/schools/search', (req, res) => {
  const q = (req.query.q || '').trim();
  if (!q) return res.json([]);
  res.json(searchSchools(q));
});

// POST /api/schools/resolve — LLM fallback for ambiguous queries
let _resolveInFlight = false;
app.post('/api/schools/resolve', async (req, res) => {
  const { query } = req.body || {};
  if (!query) return res.status(400).json({ error: 'No query provided' });

  const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY;
  if (!ANTHROPIC_API_KEY) {
    return res.status(501).json({ error: 'LLM resolution not configured (ANTHROPIC_API_KEY missing)' });
  }

  if (_resolveInFlight) {
    return res.status(429).json({ error: 'Resolve already in progress' });
  }
  _resolveInFlight = true;

  try {
    let registry;
    try { registry = JSON.parse(fs.readFileSync(SCHOOL_REGISTRY, 'utf8')); }
    catch { return res.status(500).json({ error: 'School registry not found' }); }

    const schoolNames = registry.map(s => s.name).join(', ');

    const apiResp = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
      },
      body: JSON.stringify({
        model: 'claude-sonnet-4-20250514',
        max_tokens: 200,
        messages: [{
          role: 'user',
          content: `Given this list of NCAA D1 baseball schools:\n${schoolNames}\n\nThe user searched for: "${query}"\n\nWhich school did they most likely mean? Respond with ONLY the exact school name from the list, or "NONE" if no match is reasonable. Do not explain.`,
        }],
      }),
      signal: AbortSignal.timeout(15_000),
    });

    const data = await apiResp.json();
    const answer = data.content?.[0]?.text?.trim();

    if (!answer || answer === 'NONE') {
      return res.json({ resolved: null, message: 'No match found' });
    }

    const match = registry.find(s => s.name.toLowerCase() === answer.toLowerCase());
    if (match) return res.json({ resolved: match });

    // Partial match: try includes
    const partial = registry.find(s =>
      s.name.toLowerCase().includes(answer.toLowerCase()) ||
      answer.toLowerCase().includes(s.name.toLowerCase())
    );
    if (partial) return res.json({ resolved: partial });

    res.json({ resolved: null, message: `LLM suggested "${answer}" but no registry match` });
  } catch (err) {
    console.error('[resolve] LLM error:', err.message);
    res.status(500).json({ error: `LLM resolution failed: ${err.message}` });
  } finally {
    _resolveInFlight = false;
  }
});

// POST /api/schools/import — scrape, analyse, import a school (SSE streaming)
let _importInFlight = false;
app.post('/api/schools/import', async (req, res) => {
  if (_importInFlight) {
    return res.status(409).json({ error: 'Import already in progress' });
  }

  const { ncaa_id, name } = req.body || {};
  if (!ncaa_id || !name) {
    return res.status(400).json({ error: 'Missing ncaa_id or name' });
  }

  const slug = (req.body.slug || name)
    .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');

  // SSE headers
  res.writeHead(200, {
    'Content-Type':      'text/event-stream',
    'Cache-Control':     'no-cache',
    'Connection':        'keep-alive',
    'X-Accel-Buffering': 'no',
  });

  const send = (event, data) => {
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  };

  _importInFlight = true;
  send('status', { step: 'start', message: `Importing ${name}...` });

  try {
    // 1. Scrape + compute metrics
    const entries = await importTeam(ncaa_id, name, (progress) => {
      send('progress', progress);
    });

    // 2. Enrich via APPM
    const { pitchers } = enrichPitchers(entries);
    send('progress', { step: 'enrich', message: `APPM enrichment complete`, pct: 95 });

    // 3. Write team JSON
    if (!fs.existsSync(RUNTIME_TEAMS_DIR)) fs.mkdirSync(RUNTIME_TEAMS_DIR, { recursive: true });
    const teamFile = path.join(RUNTIME_TEAMS_DIR, `${slug}.json`);
    fs.writeFileSync(teamFile, JSON.stringify(pitchers, null, 2));

    // 4. Update manifest (runtime manifest for Vercel, repo manifest locally)
    const manifestPath = IS_VERCEL ? RUNTIME_MANIFEST : TEAMS_MANIFEST;
    let manifest = [];
    if (fs.existsSync(manifestPath)) {
      try { manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8')); } catch { manifest = []; }
    }
    const existing = manifest.find(t => t.slug === slug);
    if (!existing) {
      manifest.push({ name, slug, ncaa_id });
      manifest.sort((a, b) => a.name.localeCompare(b.name));
      fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
    } else if (!existing.ncaa_id) {
      existing.ncaa_id = ncaa_id;
      fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
    }

    send('done', {
      success: true, slug, name, ncaa_id,
      pitcherCount: pitchers.length,
      pitchers,                              // full data for client-side caching
      message: `Successfully imported ${pitchers.length} pitchers for ${name}`,
    });
  } catch (err) {
    console.error('[import] Error:', err.message);
    send('done', {
      success: false,
      error: err.message,
      message: `Import failed: ${err.message}`,
    });
  } finally {
    _importInFlight = false;
    res.end();
  }

  req.on('close', () => { _importInFlight = false; });
});

// POST /api/schools/update — re-scrape latest data for an existing team (SSE streaming)
app.post('/api/schools/update', async (req, res) => {
  if (_importInFlight) {
    return res.status(409).json({ error: 'Import/update already in progress' });
  }

  const { ncaa_id, name, slug } = req.body || {};
  if (!ncaa_id || !name || !slug) {
    return res.status(400).json({ error: 'Missing ncaa_id, name, or slug' });
  }

  // SSE headers
  res.writeHead(200, {
    'Content-Type':      'text/event-stream',
    'Cache-Control':     'no-cache',
    'Connection':        'keep-alive',
    'X-Accel-Buffering': 'no',
  });

  const send = (event, data) => {
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  };

  _importInFlight = true;
  send('status', { step: 'start', message: `Updating ${name}...` });

  try {
    // 1. Re-scrape + compute metrics
    const entries = await importTeam(ncaa_id, name, (progress) => {
      send('progress', progress);
    });

    // 2. Enrich via APPM
    const { pitchers } = enrichPitchers(entries);
    send('progress', { step: 'enrich', message: `APPM enrichment complete`, pct: 95 });

    // 3. Overwrite team JSON (check both runtime + repo dirs)
    const runtimeFile = path.join(RUNTIME_TEAMS_DIR, `${slug}.json`);
    const repoFile = path.join(TEAMS_DIR, `${slug}.json`);

    if (!IS_VERCEL && fs.existsSync(repoFile)) {
      // Local: overwrite repo file directly
      fs.writeFileSync(repoFile, JSON.stringify(pitchers, null, 2));
    } else {
      // Vercel: write to runtime dir (repo is read-only)
      if (!fs.existsSync(RUNTIME_TEAMS_DIR)) fs.mkdirSync(RUNTIME_TEAMS_DIR, { recursive: true });
      fs.writeFileSync(runtimeFile, JSON.stringify(pitchers, null, 2));
    }

    send('done', {
      success: true, slug, name, ncaa_id,
      pitcherCount: pitchers.length,
      pitchers,
      message: `Successfully updated ${pitchers.length} pitchers for ${name}`,
    });
  } catch (err) {
    console.error('[update] Error:', err.message);
    send('done', {
      success: false,
      error: err.message,
      message: `Update failed: ${err.message}`,
    });
  } finally {
    _importInFlight = false;
    res.end();
  }

  req.on('close', () => { _importInFlight = false; });
});

// GET /api/crew-session — serve crew_session.json for the hex UI
app.get('/api/crew-session', (req, res) => {
  if (!fs.existsSync(CREW_SESSION)) {
    return res.status(404).json({ error: 'No crew session data. Run scout agents first.' });
  }
  try {
    const data = JSON.parse(fs.readFileSync(CREW_SESSION, 'utf8'));
    res.json(data);
  } catch (err) {
    res.status(500).json({ error: 'Failed to parse crew_session.json', details: err.message });
  }
});

// ── Vercel: export app as default; locally: start listening ───────────────────
export default app;

if (!IS_VERCEL) {
  app.listen(PORT, () => {
    const client = new TrackmanClient();
    console.log(`⚾  pitching-hub  →  http://localhost:${PORT}`);
    console.log(`    trackman: ${client.isConfigured ? `configured (team ${client.teamId})` : 'unconfigured (mock mode)'}`);
    console.log(`    auth:     ${ADMIN_USER} / ${'*'.repeat(ADMIN_PASS.length)}  (JWT, 7-day sessions)`);
  });
}
