// PHONE-133 build-003: the web call page end to end in a real browser. Headless Chrome over
// CDP with a virtual authenticator (WebAuthn.addVirtualAuthenticator), so the passkey ceremony
// is real and the server's passkeys.py checks it; the mic is Chrome's fake device playing a tone
// wav. Copied from settings@19ecdee tests/browser/drive.mjs and enrol.mjs. Run by run.sh.
// PHONE-139 adds steps 8 and 9: a page on ANOTHER origin (run.sh's scratch http server) opens the
// call top-level with target=_blank, and that new tab signs in and calls; a framed copy of the
// call is refused by frame-ancestors 'none', so it holds no page to sign in with.
// PHONE-136 adds step 10: the QR's URL, <qrbase>/#code=<code>, on a SECOND address with no passkey
// yet (qr.localhost, a fresh RP). The page fills the code in from the fragment and strikes the
// fragment from the address bar and the history; the virtual authenticator enrols; the server's
// log shows the enrol ok and never the code. The code is minted fresh by the <mint> command
// (`sessionphone web enrol`), since step 3 spent the first.
// usage: node drive.mjs <cdpPort> <base> <code> <other> <qrbase> <serverLog> <mint...>
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
const [cdpPort, base, code, other, qrbase, serverLog, ...mint] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const targets = async () => (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json();
// One CDP session on one target (a tab, or an out-of-process iframe): its cdp(), ev(), the page
// errors it threw and the CSP issues it reported.
async function attach(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise(r => ws.onopen = r);
  let id = 0; const wait = {}; const errors = [], issues = [];
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.id && wait[m.id]) { wait[m.id](m); delete wait[m.id]; }
    if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    if (m.method === "Audits.issueAdded" && m.params.issue.code === "ContentSecurityPolicyIssue") issues.push(m.params.issue.details.contentSecurityPolicyIssueDetails);
  };
  const cdp = (method, params = {}) => new Promise((res, rej) => { const i = ++id; wait[i] = m => m.error ? rej(new Error(method + ": " + JSON.stringify(m.error))) : res(m.result); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (expr, gesture) => (await cdp("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true, userGesture: !!gesture })).result.value;
  const s = { ws, cdp, ev, errors, issues };
  s.state = () => ev(`[document.getElementById('state').textContent, document.getElementById('why').textContent]`);
  s.tap = b => ev(`document.getElementById('${b}').click()`, true);
  s.visible = b => ev(`!document.getElementById('${b}').hidden`);
  s.until = async (expr, what, ms = 10000) => { for (let t = 0; t < ms; t += 100) { try { const v = await ev(expr); if (v) return v; } catch (e) {} await sleep(100); } throw new Error("timeout: " + what + "  (state " + JSON.stringify(await s.state().catch(() => null)) + ")"); };
  return s;
}
const main = await attach((await targets()).find(t => t.type === "page").webSocketDebuggerUrl);
const { ws, cdp, ev, errors, state, tap, visible, until } = main;
const results = []; let failed = 0;
function ok(cond, name, extra) { results.push((cond ? "PASS " : "FAIL ") + name + (extra ? "  " + extra : "")); if (!cond) failed++; }

await cdp("Page.enable"); await cdp("Runtime.enable"); await cdp("WebAuthn.enable"); await cdp("Audits.enable");
const { authenticatorId } = await cdp("WebAuthn.addVirtualAuthenticator", { options: { protocol: "ctap2", transport: "internal",
  hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } });
await cdp("Page.addScriptToEvaluateOnNewDocument", { source: `window.__creates=0;(()=>{const c=navigator.credentials.create.bind(navigator.credentials);navigator.credentials.create=o=>{window.__creates++;return c(o)}})();` });
const onDevice = async () => (await cdp("WebAuthn.getCredentials", { authenticatorId })).credentials.length;
const go = async () => { await cdp("Page.navigate", { url: base + "/" }); await sleep(300); await until("document.readyState==='complete'", "load"); };

// The loudest 100 ms (RMS, -1..1) the page's remote <audio> stream carries over `ms`, by a Web
// Audio AnalyserNode on that stream. Run with a gesture so the AudioContext may start.
const loudest = ms => ev(`(async () => {
  const s = document.getElementById('remote').srcObject; if (!s || !s.getAudioTracks().length) return -1;
  const ctx = new AudioContext(); await ctx.resume();
  const a = ctx.createAnalyser(); a.fftSize = 2048; ctx.createMediaStreamSource(s).connect(a);
  const buf = new Float32Array(a.fftSize); let top = 0;
  for (let t = 0; t < ${ms}; t += 100) {
    await new Promise(r => setTimeout(r, 100)); a.getFloatTimeDomainData(buf);
    let e = 0; for (const x of buf) e += x * x; top = Math.max(top, Math.sqrt(e / buf.length));
  }
  await ctx.close(); return top; })()`, true);

try {   // a step that times out still prints the results so far
  // 1. a fresh store: the page asks for the enrol code
  await go();
  await until(`document.getElementById('state').textContent==='locked'`, "locked");
  ok(await visible("code") && (await ev("document.getElementById('signin').textContent")) === "Check the code",
     "a store with no passkey for this host asks for the enrol code", JSON.stringify(await state()));
  // 2. a wrong code: refused, and no passkey ceremony is started
  await ev(`document.getElementById('code').value='WRONGCODE9'`); await tap("signin");
  await until(`document.getElementById('why').textContent.indexOf('wrong')>=0`, "wrong-code why");
  ok((await ev("window.__creates")) === 0 && (await onDevice()) === 0, "a wrong code is refused before any passkey is made",
     JSON.stringify(await state()));
  // 3. the code from `sessionphone web enrol`: pre-checked, then a second tap makes the passkey and signs in
  await ev(`document.getElementById('code').value=${JSON.stringify(" " + code.toLowerCase() + " ")}`); await tap("signin");
  await until(`document.getElementById('signin').textContent==='Make the passkey'`, "enrol-ok");
  await tap("signin");
  await until(`document.getElementById('state').textContent==='signed in'`, "signed in after enrol");
  ok((await ev("window.__creates")) === 1 && (await onDevice()) === 1, "the enrol code makes exactly one passkey and signs in",
     `creates=${await ev("window.__creates")} device=${await onDevice()}`);
  // 4. a fresh page: sign in with that passkey (login), no code asked
  await go();
  await until(`document.getElementById('signin').textContent==='Sign in' && !document.getElementById('signin').hidden`, "login offer");
  ok(!(await visible("code")), "a host with a passkey offers Sign in, with no code box");
  await tap("signin");
  await until(`document.getElementById('state').textContent==='signed in'`, "signed in by login");
  ok(await visible("call"), "the passkey signs in, and the page offers Call");
  // 4b. busy: a second socket in this page signs in with the same passkey and holds a live call;
  // the page's Call then gets "busy", and says so (its ICE must not draw a "no call" over it)
  await ev(`(async () => { window.raw = []; const s = window.rs = new WebSocket("wss://" + location.host + "/ws");
    s.onmessage = async e => { const m = JSON.parse(e.data); window.raw.push(m.type);
      if (m.type === "auth") { const a = await navigator.credentials.get({ publicKey: { challenge: unb64u(m.challenge),
          rpId: m.rpId, userVerification: "required", allowCredentials: m.allow.map(id => ({ type: "public-key", id: unb64u(id) })) } });
        s.send(JSON.stringify({ type: "auth-finish", mode: "login", id: a.id, clientDataJSON: b64u(a.response.clientDataJSON),
          authenticatorData: b64u(a.response.authenticatorData), signature: b64u(a.response.signature) })); }
      else if (m.type === "auth-ok") { const p = window.rp = new RTCPeerConnection({ iceServers: [] });
        p.addTransceiver("audio"); await p.setLocalDescription(await p.createOffer());
        s.send(JSON.stringify({ type: "call", sdp: p.localDescription.sdp })); } };
    return true; })()`, true);
  await until(`window.raw.indexOf('answer')>=0`, "a second caller live");
  await tap("call");
  await until(`document.getElementById('why').textContent.indexOf('busy')>=0`, "busy");
  await sleep(1000);                                  // any late "error" would land by now
  ok((await state())[1].indexOf("busy") >= 0 && await visible("call"), "a second caller gets busy, and the page says so and offers Call",
     JSON.stringify(await state()));
  await ev(`window.rs.close(), window.rp.close(), true`);
  await sleep(500);                                   // the socket's close ends that call on the server
  // 5. Call: live call:N
  await tap("call");
  const live = await until(`/^live call:\\d+$/.test(document.getElementById('state').textContent) && document.getElementById('state').textContent`, "live call:N");
  ok(true, "Call reaches " + JSON.stringify(live));
  ok((await ev("document.getElementById('call').textContent")) === "Hang up", "the Call button became Hang up");
  // 6. echo mode: the caller hears the tone back, about 1 s later
  const top = await loudest(5000);
  const stats = await ev(`pc.getStats().then(r => { const o = {}; r.forEach(s => {
    if (s.type === "inbound-rtp" && s.kind === "audio") Object.assign(o, { in_bytes: s.bytesReceived, in_energy: s.totalAudioEnergy });
    if (s.type === "outbound-rtp" && s.kind === "audio") o.out_bytes = s.bytesSent;
    if (s.type === "media-source" && s.kind === "audio") o.mic_energy = s.totalAudioEnergy; }); return JSON.stringify(o); })`);
  ok(top > 0.02, "echo mode: the page's remote audio is non-silent (AnalyserNode)", `loudest RMS ${top.toFixed(4)}  rtp ${stats}`);
  // 7. Hang up ends the call; the socket is replaced, so the page is ready to sign in again
  await tap("call");
  await until(`document.getElementById('state').textContent==='ended'`, "ended");
  ok((await ev("document.getElementById('why').textContent")) === "hangup", "Hang up ends the call", JSON.stringify(await state()));
  ok((await ev("!document.getElementById('remote').srcObject")) === true, "the remote audio is let go");
  await until(`document.getElementById('state').textContent==='locked' && !document.getElementById('signin').hidden`, "a fresh sign-in");
  ok(true, "after the call a fresh socket offers Sign in again", JSON.stringify(await state()));
  ok(errors.length === 0, "no uncaught page errors", JSON.stringify(errors));

  // 8. PHONE-139 SC4: a page on ANOTHER origin links to the call with target=_blank (Talk's phone
  // button, design §5.4). The new tab is top-level, so it signs itself in and calls; the link
  // hands it nothing (no opener, no referrer, the bare URL).
  await cdp("Page.navigate", { url: other + "/link.html" });
  await until(`location.origin===${JSON.stringify(other)} && document.readyState==='complete'`, "the other origin's page");
  ok((await ev(`[document.getElementById('open').target, document.getElementById('open').rel].join(' ')`)) === "_blank noopener noreferrer",
     "another origin's page links to the call with target=_blank rel=noopener noreferrer", other);
  const known = new Set((await targets()).map(t => t.id));
  await tap("open");
  let opened;
  for (let t = 0; t < 10000 && !opened; t += 100) {
    opened = (await targets()).find(x => x.type === "page" && !known.has(x.id) && x.url.startsWith(base));
    if (!opened) await sleep(100);
  }
  if (!opened) throw new Error("timeout: the link opened no new tab at " + base);
  const tab = await attach(opened.webSocketDebuggerUrl);
  await tab.cdp("Page.enable"); await tab.cdp("Runtime.enable"); await tab.cdp("WebAuthn.enable"); await tab.cdp("Page.bringToFront");
  // The virtual authenticator belongs to the first tab: give the new tab its own, holding the
  // same passkey (as a phone's platform authenticator would hold it for every tab).
  const { authenticatorId: tabAuth } = await tab.cdp("WebAuthn.addVirtualAuthenticator", { options: { protocol: "ctap2", transport: "internal",
    hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } });
  for (const credential of (await cdp("WebAuthn.getCredentials", { authenticatorId })).credentials)
    await tab.cdp("WebAuthn.addCredential", { authenticatorId: tabAuth, credential });
  await tab.until(`document.readyState==='complete' && document.getElementById('signin').textContent==='Sign in' && !document.getElementById('signin').hidden`, "the new tab's login offer");
  ok((await tab.ev(`[location.href, window.opener === null, document.referrer, window.top === window].join(' ')`)) === `${base}/ true  true`,
     "the new tab is top-level at the bare URL, with no opener and no referrer", await tab.ev("location.href"));
  await tab.tap("signin");
  await tab.until(`document.getElementById('state').textContent==='signed in'`, "the new tab signed in");
  ok(await tab.visible("call"), "the new tab signs itself in with the call's own passkey, and offers Call");
  await tab.tap("call");
  const tabLive = await tab.until(`/^live call:\\d+$/.test(document.getElementById('state').textContent) && document.getElementById('state').textContent`, "the new tab's live call:N");
  ok(true, "the new tab calls: " + JSON.stringify(tabLive));
  await tab.tap("call");
  await tab.until(`document.getElementById('state').textContent==='ended'`, "the new tab hung up");
  ok(tab.errors.length === 0, "no uncaught page errors in the new tab", JSON.stringify(tab.errors));
  await cdp("Target.closeTarget", { targetId: opened.id }); tab.ws.close();

  // 9. PHONE-139 SC4: a framed copy of the call cannot sign in. frame-ancestors 'none' (do-001)
  // stops the page loading in the frame at all, so there is no button to tap and no socket.
  main.issues.length = 0;
  await cdp("Page.navigate", { url: other + "/frame.html" });
  await until(`location.origin===${JSON.stringify(other)} && document.readyState==='complete'`, "the framing page");
  await sleep(1000);                                   // the frame's response, and its refusal, land by now
  const refused = main.issues.find(i => i.violatedDirective === "frame-ancestors" && i.blockedURL.startsWith(base));
  ok(!!refused, "the browser refuses to frame the call (frame-ancestors)", JSON.stringify(main.issues));
  const framed = (await targets()).find(x => x.type === "iframe" && x.url.startsWith(base));
  if (framed) {
    const f = await attach(framed.webSocketDebuggerUrl);
    await f.cdp("Runtime.enable");
    ok((await f.ev(`!document.getElementById('signin') && !document.getElementById('state')`)) === true,
       "the framed copy holds no call page: nothing to sign in with", await f.ev("location.href"));
    f.ws.close();
  } else ok(false, "the framed copy holds no call page: nothing to sign in with", "no iframe target to inspect");

  // 10. PHONE-136 SC4: the QR's URL. A fresh code, the page opened at <qrbase>/#code=<code>.
  const minted = execFileSync(mint[0], mint.slice(1), { encoding: "utf8" });
  const qrCode = (/^enrol code:\s+(\S+)/m.exec(minted) || [])[1];
  if (!qrCode) throw new Error("the mint printed no enrol code: " + minted);
  await cdp("Page.navigate", { url: `${qrbase}/#code=${qrCode}` });
  await until(`location.origin===${JSON.stringify(qrbase)} && document.readyState==='complete'`, "the QR's page");
  await until(`document.getElementById('state').textContent==='locked' && !document.getElementById('signin').hidden`, "the QR page's enrol offer");
  ok((await ev("document.getElementById('code').value")) === qrCode && await visible("code")
     && (await ev("document.getElementById('signin').textContent")) === "Check the code"
     && (await state())[1].indexOf("from the QR") >= 0,
     "the fragment fills the enrol code in, after the offer's reset", JSON.stringify(await state()));
  const hist = (await cdp("Page.getNavigationHistory")).entries.map(e => e.url);
  ok((await ev("location.href")) === qrbase + "/" && (await ev("location.hash")) === ""
     && !hist.some(u => u.indexOf("code=") >= 0 || u.indexOf(qrCode) >= 0),
     "the address bar and the history lost #code", JSON.stringify(hist));
  await tap("signin");
  await until(`document.getElementById('signin').textContent==='Make the passkey'`, "the QR code's enrol-ok");
  await tap("signin");
  await until(`document.getElementById('state').textContent==='signed in'`, "signed in after the QR's enrol");
  ok((await ev("window.__creates")) === 1 && (await onDevice()) === 2,
     "the QR's code enrols one passkey with the virtual authenticator, and signs in",
     `creates=${await ev("window.__creates")} device=${await onDevice()}`);
  const log = readFileSync(serverLog, "utf8");
  ok(log.includes(`"auth": "enrol", "origin": "${qrbase}", "result": "ok"`) && !log.includes(qrCode),
     "the server logged the QR's enrol ok, and never its code");
  ok(errors.length === 0, "no uncaught page errors after the QR's enrol", JSON.stringify(errors));
} catch (e) { ok(false, String(e && e.message || e)); }
console.log(results.join("\n")); ws.close(); process.exit(failed ? 1 : 0);
