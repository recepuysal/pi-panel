// YouTube / YouTube Music yayin alicisi: telefonda "Pi Panel" olarak gorunur, sesi Bluetooth'a calar.
// YouTube uygulamasindan gelen yayinda video, dashboard'un YouTube sayfasindaki yerinde oynar (mpv DRM + overlay).
//  - yt-cast-receiver: DIAL + YouTube yayin oturumu (telefonla konusma)
//  - resolver.py (surekli acik yt-dlp): sarkinin ses adresi ve bilgileri
//  - mpv (JSON IPC): calma, duraklatma, ses
//  - /run/ytcast/now.json: ekran (dashboard) icin durum
//  - 127.0.0.1:8100: ekrandaki dugmeler (play, pause, next, previous, mute, volume?level=)
import YouTubeCastReceiver, { Player, Constants } from 'yt-cast-receiver';
import { spawn, execFile } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import http from 'node:http';
import path from 'node:path';

const HERE = path.dirname(new URL(import.meta.url).pathname);
const RUN = process.env.RUNTIME_DIRECTORY || '/run/ytcast';
const SOCK = path.join(RUN, 'mpv.sock');
const NOW = path.join(RUN, 'now.json');
const AUDIO = 'alsa/bluealsa:DEV=00:00:00:00:00:00,PROFILE=a2dp';
const HDMI_AUDIO = 'alsa/plughw:CARD=vc4hdmi0';
const log = (...a) => console.log(new Date().toTimeString().slice(0, 8), ...a);

// ------------------------------------------------------------------ cozucu (yt-dlp)
class Resolver {
  constructor() {
    this.n = 0;
    this.waiting = new Map();
    this.start();
  }

  start() {
    this.proc = spawn('python3', [path.join(HERE, 'resolver.py')], { stdio: ['pipe', 'pipe', 'inherit'] });
    let buf = '';
    this.proc.stdout.on('data', (d) => {
      buf += d;
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        let msg; try { msg = JSON.parse(line); } catch { continue; }
        if (msg.ready) log('cozucu hazir:', msg.runtime);
        else if (this.waiting.has(msg.id)) { this.waiting.get(msg.id)(msg); this.waiting.delete(msg.id); }
      }
    });
    this.proc.on('exit', (code) => {
      log('cozucu kapandi', code);
      for (const done of this.waiting.values()) done({ error: 'cozucu kapandi' });
      this.waiting.clear();
      setTimeout(() => this.start(), 3000);
    });
  }

  resolve(v, video = false) {
    return new Promise((done) => {
      const id = ++this.n;
      this.waiting.set(id, done);
      this.proc.stdin.write(JSON.stringify({ id, v, video }) + '\n');
    });
  }
}
const resolver = new Resolver();

// Bluetooth kulaklik/hoparlor bagliysa ona, degilse ekranin (HDMI) hoparlorune
function audioDevice() {
  return new Promise((done) => execFile('bluealsa-cli', ['list-pcms'], { timeout: 4000 }, (err, out) =>
    done(!err && /\/a2dp\w*\/sink/.test(out || '') ? AUDIO : HDMI_AUDIO))); // .../a2dpsrc/sink
}

// boyut bilinmiyorsa ilk baytin yanitindan ogren
async function remoteSize(url, headers) {
  const h = await fetch(url, { headers: { ...headers, Range: 'bytes=0-0' } }).catch(() => null);
  return Number(((h && h.headers.get('content-range')) || '').split('/')[1]) || 0;
}

// ------------------------------------------------------------------ yerel aktarici
// YouTube tek parca / buyuk aralik isteklerine 403 verebiliyor; 1 MB'lik araliklar her zaman kabul.
// mpv buraya baglanir; biz sesi parca parca indirip akitiriz (ileri/geri sarma icin Range destekli).
const STREAMS = new Map();
const CHUNK = 1 << 20;
http.createServer(async (req, res) => {
  const s = STREAMS.get(new URL(req.url, 'http://x').pathname.slice(1));
  if (!s || !s.size) { res.writeHead(404); return res.end(); }
  let start = 0, end = s.size - 1;
  const m = /bytes=(\d+)-(\d*)/.exec(req.headers.range || '');
  if (m) { start = +m[1]; if (m[2]) end = Math.min(+m[2], end); }
  if (start > end) { res.writeHead(416); return res.end(); }
  res.writeHead(m ? 206 : 200, { 'Content-Type': s.mime, 'Content-Length': end - start + 1, 'Accept-Ranges': 'bytes',
    ...(m ? { 'Content-Range': `bytes ${start}-${end}/${s.size}` } : {}) });
  let closed = false;
  res.on('close', () => { closed = true; });
  try {
    for (let pos = start; pos <= end && !closed; pos += CHUNK) {
      const to = Math.min(pos + CHUNK - 1, end);
      let r;
      for (let t = 0; t < 3; t++) { // kisa kopmalarda tekrar dene
        r = await fetch(s.url, { headers: { ...s.headers, Range: `bytes=${pos}-${to}` } }).catch(() => null);
        if (r && r.ok) break;
      }
      if (!r || !r.ok) throw new Error('parca alinamadi: ' + (r && r.status));
      const buf = Buffer.from(await r.arrayBuffer());
      if (!closed && !res.write(buf)) await new Promise((ok) => { res.once('drain', ok); res.once('close', ok); });
    }
    res.end();
  } catch (e) {
    log('aktarici:', e.message);
    res.destroy();
  }
}).listen(8101, '127.0.0.1');

// ------------------------------------------------------------------ mpv
class Mpv {
  constructor() {
    this.id = 0;
    this.pending = new Map();
    this.onEvent = () => {};
    this.start();
  }

  start() {
    try { fs.unlinkSync(SOCK); } catch {}
    this.proc = spawn('mpv', ['--idle=yes', '--ytdl=no', '--vid=no', '--no-terminal', '--input-ipc-server=' + SOCK,
      // video ekrana dogrudan (DRM), sesle senkron. Yeri ve gorunurlugu dashboard belirler: YouTube sayfasi
      // acilinca vid=auto + kenar paylari + sayfa mpv'ye saydam katman (overlay-add) olarak verilir.
      '--vo=gpu', '--gpu-context=drm', '--hwdec=no', '--profile=fast', '--force-window=no', '--osd-level=0',
      '--ao=alsa', '--audio-device=' + AUDIO, '--volume=60', '--cache=yes', '--demuxer-max-bytes=8MiB'],
    { stdio: 'ignore' });
    this.proc.on('exit', (code) => { log('mpv kapandi', code); setTimeout(() => this.start(), 2000); });
    const connect = () => {
      const s = net.createConnection(SOCK);
      s.on('error', () => setTimeout(connect, 300));
      s.on('connect', () => {
        this.sock = s;
        let buf = '';
        s.on('data', (d) => {
          buf += d;
          let i;
          while ((i = buf.indexOf('\n')) >= 0) {
            const line = buf.slice(0, i); buf = buf.slice(i + 1);
            let msg; try { msg = JSON.parse(line); } catch { continue; }
            if (msg.request_id && this.pending.has(msg.request_id)) {
              this.pending.get(msg.request_id)(msg); this.pending.delete(msg.request_id);
            } else if (msg.event) this.onEvent(msg);
          }
        });
      });
    };
    setTimeout(connect, 300);
  }

  cmd(...command) {
    return new Promise((resolve) => {
      if (!this.sock) return resolve({ error: 'not connected' });
      const request_id = ++this.id;
      this.pending.set(request_id, resolve);
      this.sock.write(JSON.stringify({ command, request_id }) + '\n');
      setTimeout(() => { if (this.pending.delete(request_id)) resolve({ error: 'timeout' }); }, 5000);
    });
  }

  async get(prop) { return (await this.cmd('get_property', prop)).data; }
  set(prop, val) { return this.cmd('set_property', prop, val); }
}

// ------------------------------------------------------------------ oynatici
class MpvPlayer extends Player {
  constructor() {
    super();
    this.mpv = new Mpv();
    this.vol = { level: 60, muted: false };
    this.info = {};
    this.loadId = 0;
    this.mpv.onEvent = (ev) => {
      if (ev.event === 'end-file' && ev.reason === 'eof' && this.status === Constants.PLAYER_STATUSES.PLAYING) {
        log('sarki bitti, siradaki');
        this.pause().then(() => this.next()).catch((e) => log('next', e.message));
      }
    };
    this.on('state', () => this.writeState());
  }

  async doPlay(video, position) {
    const id = ++this.loadId;
    const withVideo = video.client?.key === Constants.CLIENTS.YT.key; // YouTube uygulamasi -> ekrana video
    log('cal', video.id, withVideo ? '(video)' : '');
    this.info = { name: '', artists: [], album: '', cover: `https://i.ytimg.com/vi/${video.id}/hqdefault.jpg` };
    this.writeState('loading');
    const r = await resolver.resolve(video.id, withVideo);
    if (id !== this.loadId) return false; // bu arada baska sarki istendi
    if (r.error) { log('calinamadi:', r.error); return false; }
    const size = r.size || await remoteSize(r.url, r.headers);
    this.info = { name: r.name, artists: r.artists, album: r.album, duration: r.duration, cover: r.cover, video: withVideo };
    STREAMS.set('cur', { url: r.url, headers: r.headers, size, mime: r.ext === 'webm' ? 'audio/webm' : 'audio/mp4' });
    let local = `http://127.0.0.1:8101/cur?${id}`; // YouTube'a 1 MB'lik parcalarla giden yerel aktarici
    const opts = position > 0 ? [`start=${position}`] : [];
    if (withVideo) {
      opts.push('vid=no'); // dashboard YouTube sayfasinda acar
      if (r.vurl) { // ayri video + ses: ses ek dosya olarak
        STREAMS.set('curv', { url: r.vurl, headers: r.vheaders, size: r.vsize || await remoteSize(r.vurl, r.vheaders), mime: 'video/mp4' });
        opts.push(`audio-files-append=${local}`); // audio-files= adresi ':' den boluyor (yol listesi) -> ses yok
        local = `http://127.0.0.1:8101/curv?${id}`;
      }
    }
    await this.mpv.set('audio-device', await audioDevice());
    await this.mpv.cmd('loadfile', local, 'replace', -1, opts.join(','));
    await this.mpv.set('pause', false);
    log('caliyor:', r.name, '-', r.artists.join(', '));
    return true;
  }

  async doPause() { await this.mpv.set('pause', true); return true; }
  async doResume() { await this.mpv.set('pause', false); return true; }
  async doStop() { this.loadId++; await this.mpv.cmd('stop'); return true; }
  async doSeek(position) { await this.mpv.cmd('seek', position, 'absolute'); return true; }
  async doSetVolume(volume) {
    this.vol = { level: volume.level, muted: volume.muted };
    await this.mpv.set('volume', volume.level);
    await this.mpv.set('mute', volume.muted);
    this.writeState();
    return true;
  }
  async doGetVolume() { return this.vol; }
  async doGetPosition() { return (await this.mpv.get('time-pos')) || 0; }
  async doGetDuration() { return (await this.mpv.get('duration')) || this.info.duration || 0; }

  async writeState(force) {
    const S = Constants.PLAYER_STATUSES;
    const state = force || { [S.PLAYING]: 'playing', [S.PAUSED]: 'paused', [S.LOADING]: 'loading' }[this.status] || 'stopped';
    const pos = state === 'loading' ? 0 : await this.doGetPosition().catch(() => 0);
    const st = { state, ...this.info, duration: Math.round((this.info.duration || 0) * 1000),
      position: Math.round(pos * 1000), volume: this.vol.level, muted: this.vol.muted, time: Date.now() / 1000 };
    try {
      fs.writeFileSync(NOW + '.tmp', JSON.stringify(st));
      fs.renameSync(NOW + '.tmp', NOW);
    } catch (e) { log('durum yazilamadi', e.message); }
  }
}

// ------------------------------------------------------------------ alici
const player = new MpvPlayer();
const receiver = new YouTubeCastReceiver(player, {
  device: { name: 'Pi Panel', screenName: 'Pi Panel', brand: 'Raspberry Pi', model: 'Pi 4' },
  dial: { port: 8008 },
  logLevel: Constants.LOG_LEVELS.WARN,
});
receiver.on('senderConnect', (s) => log('baglandi:', s.name));
receiver.on('senderDisconnect', (s) => log('ayrildi:', s.name));
receiver.on('error', (e) => log('alici hatasi:', e.message));
// ag kopunca alici kendini durduruyor ve telefonda gorunmez oluyor: cik, systemd yeniden baslatsin
receiver.on('terminate', (e) => { log('alici durdu:', e && e.message); process.exit(1); });
await receiver.start();
log('Pi Panel hazir');

// TV kodu: yerel ag cihaz bulmayi (multicast) engelliyorsa telefon Ayarlar > TV'de izle > TV kodu girin ile baglanir.
// Kod birkac dakikada bir yenilenir; /run/ytcast/pair.json (ekran gosterebilir) ve 127.0.0.1:8100/pair
const pairing = receiver.getPairingCodeRequestService();
pairing.on('response', (code) => {
  player.pairCode = code.replace(/(\d{3})(?=\d)/g, '$1 ');
  log('TV kodu:', player.pairCode);
  try { fs.writeFileSync(path.join(RUN, 'pair.json'), JSON.stringify({ code: player.pairCode, time: Date.now() / 1000 })); } catch {}
});
pairing.on('error', (e) => log('TV kodu hatasi:', e.message));
pairing.start();
player.writeState();

// calarken kulaklik baglanir/ayrilirsa ses ona gecsin
setInterval(async () => {
  if (player.status !== Constants.PLAYER_STATUSES.PLAYING) return;
  const dev = await audioDevice();
  if (dev !== await player.mpv.get('audio-device')) {
    log('ses cikisi:', dev === AUDIO ? 'Bluetooth' : 'HDMI');
    await player.mpv.set('audio-device', dev);
  }
}, 5000);

// ilerleme bilgisi tazelensin (ekran tahmin ediyor; arada bir duzeltme)
setInterval(() => { if (player.status === Constants.PLAYER_STATUSES.PLAYING) player.writeState(); }, 15000);

// ------------------------------------------------------------------ ekran dugmeleri
http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  try {
    switch (url.pathname) {
      case '/play': await player.resume(); break;
      case '/pause': await player.pause(); break;
      case '/stop': await player.stop(); break;
      case '/seek': // ekrandaki ilerleme cubugu: /seek?pos=<saniye>
        await player.seek(Math.max(0, +url.searchParams.get('pos') || 0));
        await player.writeState();
        break;
      case '/pair': return res.end(player.pairCode || '');
      case '/next': await player.next(); break;
      case '/previous': await player.previous(); break;
      case '/mute': await player.setVolume({ level: player.vol.level, muted: !player.vol.muted }); break;
      case '/test': // telefonsuz deneme: /test?v=<id>[&video=1]
        await player.doPlay({ id: url.searchParams.get('v'),
          client: url.searchParams.get('video') ? Constants.CLIENTS.YT : Constants.CLIENTS.YTMUSIC }, 0);
        await player.notifyExternalStateChange(Constants.PLAYER_STATUSES.PLAYING); // telefonsuz: durdur/duraklat calissin
        break;
      case '/volume':
        await player.setVolume({ level: Math.max(0, Math.min(100, +url.searchParams.get('level'))), muted: false });
        break;
      default: res.writeHead(404); return res.end();
    }
    res.end('ok');
  } catch (e) {
    res.writeHead(500); res.end(e.message);
  }
}).listen(8100, '127.0.0.1');

process.on('SIGTERM', async () => { await receiver.stop().catch(() => {}); process.exit(0); });
