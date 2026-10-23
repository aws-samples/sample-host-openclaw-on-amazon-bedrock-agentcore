/**
 * Tests for the agentcore-browser destination policy (skills/agentcore-browser/url-policy.js)
 * and its use by navigate / interact / screenshot.
 *
 * The real-browser suite at the end launches a local headless Chromium when
 * playwright-core can find one and is skipped otherwise. It needs no network:
 * Chromium maps the test hostnames to a 127.0.0.1 server, and the policy is
 * given a fake resolver.
 * Run: cd bridge && node --test agentcore-browser-url-policy.test.js
 */
const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const http = require("http");
const path = require("path");
const { execFileSync } = require("child_process");

const {
  createUrlPolicy,
  precheckUrl,
  installRequestGuard,
  assertPageAllowed,
  isBlockedIp,
  UrlPolicyError,
} = require("./skills/agentcore-browser/url-policy");

const PUBLIC_V4 = "93.184.215.14";
const PUBLIC_V6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c";

function fakeLookup(table) {
  return async (host) => {
    if (!(host in table)) {
      const err = new Error("not found");
      err.code = "ENOTFOUND";
      throw err;
    }
    return table[host].map((address) => ({ address, family: address.includes(":") ? 6 : 4 }));
  };
}

describe("url-policy isBlockedIp", () => {
  const blocked = [
    "127.0.0.1", "127.255.255.254", "0.0.0.0", "10.0.0.2", "10.0.1.15", "172.16.0.1", "172.31.255.255",
    "192.168.1.1", "100.64.0.1", "169.254.169.254", "169.254.170.2", "169.254.169.253",
    "224.0.0.1", "255.255.255.255", "198.18.0.1", "192.0.2.10",
    "::", "::1", "fe80::1", "fd00:ec2::254", "fc00::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:a9fe:a9fe", "::ffff:10.0.0.1", "::a9fe:a9fe",
    "64:ff9b::a9fe:a9fe", "64:ff9b::10.0.0.1", "2002:a9fe:a9fe::1", "2002:0a00:0001::",
    "not-an-ip",
  ];
  const allowed = [
    PUBLIC_V4, "1.1.1.1", "8.8.8.8", "172.32.0.1", "100.128.0.1", "11.0.0.1",
    PUBLIC_V6, "2600:1f18::1", "::ffff:8.8.8.8", "64:ff9b::808:808", "2002:0808:0808::1",
  ];
  for (const ip of blocked) it(`blocks ${ip}`, () => assert.equal(isBlockedIp(ip), true));
  for (const ip of allowed) it(`allows ${ip}`, () => assert.equal(isBlockedIp(ip), false));
});

describe("url-policy precheckUrl", () => {
  const rejected = [
    ["file:///etc/passwd", /scheme not allowed: file/],
    ["file:///proc/self/environ", /scheme not allowed: file/],
    ["data:text/html,hi", /scheme not allowed: data/],
    ["javascript:alert(1)", /scheme not allowed: javascript/],
    ["chrome://version", /scheme not allowed: chrome/],
    ["view-source:https://example.com", /scheme not allowed: view-source/],
    ["ftp://example.com/x", /scheme not allowed: ftp/],
    ["blob:https://example.com/uuid", /scheme not allowed: blob/],
    ["http://169.254.169.254/latest/meta-data/", /not allowed/],
    ["http://169.254.169.254./", /not allowed/],
    ["http://0xA9FEA9FE/", /not allowed/],
    ["http://2852039166/", /not allowed/],
    ["http://[::ffff:169.254.169.254]/", /not allowed/],
    ["http://[fd00:ec2::254]/", /not allowed/],
    ["http://127.1:9222/json", /not allowed/],
    ["http://0/", /not allowed/],
    ["http://localhost:8080/", /not allowed: localhost/],
    ["http://LOCALHOST./", /not allowed: localhost/],
    ["http://foo.localhost/", /not allowed/],
    ["http://10.0.0.5/", /not allowed/],
    ["not a url", /not a valid absolute URL/],
  ];
  for (const [url, re] of rejected) {
    it(`rejects ${url}`, () => {
      assert.throws(() => precheckUrl(url), (err) => err instanceof UrlPolicyError && re.test(err.message));
    });
  }

  it("accepts ordinary public URLs", () => {
    for (const url of ["https://example.com", "http://example.com:8080/a?b=c", "https://1.1.1.1/", `http://[${PUBLIC_V6}]/`]) {
      assert.doesNotThrow(() => precheckUrl(url), url);
    }
  });
});

describe("url-policy checkUrl (DNS)", () => {
  const policy = createUrlPolicy({
    lookup: fakeLookup({
      "example.com": [PUBLIC_V4, PUBLIC_V6],
      "rebind.example": [PUBLIC_V4, "169.254.169.254"],
      "internal.example": ["10.0.12.7"],
      "v6meta.example": ["fd00:ec2::254"],
      "mapped.example": ["::ffff:127.0.0.1"],
    }),
  });

  it("allows a host whose every address is public", async () => {
    assert.equal(await policy.checkUrl("https://example.com/path"), "https://example.com/path");
  });

  for (const host of ["rebind.example", "internal.example", "v6meta.example", "mapped.example"]) {
    it(`rejects ${host} (resolves to a non-public address)`, async () => {
      await assert.rejects(policy.checkUrl(`https://${host}/`), /resolves to a non-public address/);
    });
  }

  it("rejects a host that does not resolve", async () => {
    await assert.rejects(policy.checkUrl("https://nxdomain.example/"), /could not resolve nxdomain.example \(ENOTFOUND\)/);
  });

  it("resolves each host once per process", async () => {
    let calls = 0;
    const counted = createUrlPolicy({ lookup: async () => { calls++; return [{ address: PUBLIC_V4, family: 4 }]; } });
    await counted.checkUrl("https://a.example/1");
    await counted.checkUrl("https://a.example/2");
    await counted.checkUrl("http://a.example/3");
    assert.equal(calls, 1);
  });
});

describe("url-policy guards (mocked Playwright objects)", () => {
  const policy = createUrlPolicy({ lookup: fakeLookup({ "example.com": [PUBLIC_V4], "internal.example": ["10.0.0.9"] }) });

  async function routeOutcome(url) {
    let handler;
    await installRequestGuard({ route: async (pattern, fn) => { assert.equal(pattern, "**/*"); handler = fn; } }, policy);
    let outcome;
    await handler({
      request: () => ({ url: () => url }),
      continue: async () => { outcome = "continue"; },
      abort: async (code) => { outcome = `abort:${code}`; },
    });
    return outcome;
  }

  it("continues public requests", async () => {
    assert.equal(await routeOutcome("https://example.com/app.js"), "continue");
  });

  it("aborts requests to blocked destinations and schemes", async () => {
    assert.equal(await routeOutcome("http://169.254.169.254/latest/api/token"), "abort:blockedbyclient");
    assert.equal(await routeOutcome("https://internal.example/"), "abort:blockedbyclient");
    assert.equal(await routeOutcome("file:///etc/hosts"), "abort:blockedbyclient");
  });

  it("lets page-local data: and blob: requests through", async () => {
    assert.equal(await routeOutcome("data:image/png;base64,AAAA"), "continue");
    assert.equal(await routeOutcome("blob:https://example.com/0b6c"), "continue");
  });

  function fakePage(urls) {
    const visited = [];
    return {
      visited,
      frames: () => urls.map((u) => ({ url: () => u })),
      goto: async (u) => { visited.push(u); },
    };
  }

  it("accepts a page whose frames are public or inert", async () => {
    const page = fakePage(["https://example.com/", "about:blank", "about:srcdoc", "chrome-error://chromewebdata/"]);
    await assertPageAllowed(page, policy);
    assert.deepEqual(page.visited, []);
  });

  it("blanks the page and throws when any frame is on a blocked destination", async () => {
    const page = fakePage(["https://example.com/", "http://169.254.169.254/latest/meta-data/"]);
    await assert.rejects(assertPageAllowed(page, policy), UrlPolicyError);
    assert.deepEqual(page.visited, ["about:blank"]);
  });
});

describe("browser skills reject blocked URLs before connecting", () => {
  const SESSION = "/tmp/agentcore-browser-session.json";
  before(() => { try { fs.unlinkSync(SESSION); } catch {} });

  function navigate(url) {
    const out = execFileSync("node", [path.join(__dirname, "skills/agentcore-browser/navigate.js"), JSON.stringify({ url })], {
      encoding: "utf8",
    });
    return JSON.parse(out.trim());
  }

  for (const url of ["file:///etc/passwd", "http://169.254.169.254/latest/meta-data/iam/", "http://[::ffff:a9fe:a9fe]/",
    "http://localhost:9222/json/version", "data:text/html,<b>x</b>", "http://10.0.0.2/"]) {
    it(`navigate refuses ${url}`, () => {
      const res = navigate(url);
      assert.match(res.error, /^url rejected: /);
    });
  }
});

// --- Real browser: redirects, subresources and frames ----------------------

function findChromium() {
  try {
    const { chromium } = require("playwright-core");
    const exe = process.env.PLAYWRIGHT_CHROMIUM_PATH || chromium.executablePath();
    return exe && fs.existsSync(exe) ? { chromium, exe } : null;
  } catch {
    return null;
  }
}

const found = findChromium();

describe("url-policy with a real Chromium", { skip: found ? false : "no local Chromium for playwright-core" }, () => {
  let server, port, browser, context;
  const hits = [];

  before(async () => {
    server = http.createServer((req, res) => {
      const host = (req.headers.host || "").split(":")[0];
      hits.push(`${host}${req.url}`);
      const html = (body) => { res.writeHead(200, { "content-type": "text/html" }); res.end(body); };
      if (host === "internal.test") return html("<title>secret</title><body>internal-secret-value</body>");
      if (req.url === "/plain") return html("<title>ok</title><body>public page</body>");
      if (req.url === "/redirect") { res.writeHead(302, { location: `http://internal.test:${port}/secret` }); return res.end(); }
      if (req.url === "/img") return html(`<body>img<img src="http://internal.test:${port}/pixel"></body>`);
      if (req.url === "/frame") return html(`<body>frame<iframe src="http://public.test:${port}/redirect"></iframe></body>`);
      res.writeHead(404); res.end();
    });
    await new Promise((r) => server.listen(0, "127.0.0.1", r));
    port = server.address().port;
    browser = await found.chromium.launch({
      executablePath: found.exe,
      args: ["--host-resolver-rules=MAP public.test 127.0.0.1, MAP internal.test 127.0.0.1"],
    });
    context = await browser.newContext();
    const policy = createUrlPolicy({
      lookup: fakeLookup({ "public.test": [PUBLIC_V4], "internal.test": ["169.254.169.254"] }),
    });
    await installRequestGuard(context, policy);
    context._testPolicy = policy;
  });

  after(async () => {
    if (browser) await browser.close();
    if (server) await new Promise((r) => server.close(r));
  });

  it("loads an ordinary page", async () => {
    const page = await context.newPage();
    await page.goto(`http://public.test:${port}/plain`);
    await assertPageAllowed(page, context._testPolicy);
    assert.equal(await page.title(), "ok");
    await page.close();
  });

  it("blocks direct navigation to a blocked host without sending the request", async () => {
    const page = await context.newPage();
    hits.length = 0;
    await assert.rejects(page.goto(`http://internal.test:${port}/secret`), /ERR_BLOCKED_BY_CLIENT/);
    assert.deepEqual(hits.filter((h) => h.startsWith("internal.test")), []);
    await page.close();
  });

  it("blocks subresources on a blocked host", async () => {
    const page = await context.newPage();
    hits.length = 0;
    await page.goto(`http://public.test:${port}/img`, { waitUntil: "load" });
    assert.deepEqual(hits.filter((h) => h.startsWith("internal.test")), []);
    await page.close();
  });

  it("refuses a page that a redirect moved onto a blocked host, and blanks it", async () => {
    const page = await context.newPage();
    await page.goto(`http://public.test:${port}/redirect`);
    // Playwright routes only the first URL of a redirect chain, so the final
    // URL check is what keeps the content from being returned.
    await assert.rejects(assertPageAllowed(page, context._testPolicy), UrlPolicyError);
    assert.equal(page.url(), "about:blank");
    await page.close();
  });

  it("refuses a page whose iframe was redirected onto a blocked host", async () => {
    const page = await context.newPage();
    await page.goto(`http://public.test:${port}/frame`, { waitUntil: "load" });
    await assert.rejects(assertPageAllowed(page, context._testPolicy), UrlPolicyError);
    await page.close();
  });
});
