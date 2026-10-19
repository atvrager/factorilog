#!/usr/bin/env node
import http from 'http';
import fs from 'fs';
import path from 'path';
import { createRequire } from 'module';

const bpInput = process.argv[2];
const outPath = process.argv[3] || 'blueprint.png';
const distDir = process.argv[4];

if (!bpInput || !distDir) {
    console.error('Usage: render_blueprint.js <blueprint_string_or_file> <output_png_path> <dist_dir>');
    process.exit(1);
}

const req = createRequire(path.resolve(distDir, '../../package.json'));
const puppeteer = req('puppeteer-core');

let bpStr = bpInput.trim();
if (fs.existsSync(bpInput)) {
    bpStr = fs.readFileSync(bpInput, 'utf8').trim();
}

let receivedPngBuffer = null;
const server = http.createServer({ maxHeaderSize: 32 * 1024 * 1024 }, (req, res) => {
    const parsedUrl = new URL(req.url, 'http://127.0.0.1');
    if (parsedUrl.pathname === '/corsproxy') {
        res.writeHead(200, { 'Content-Type': 'text/plain', 'Access-Control-Allow-Origin': '*' });
        res.end(bpStr);
        return;
    }
    if (parsedUrl.pathname === '/upload' && req.method === 'POST') {
        const chunks = [];
        req.on('data', chunk => chunks.push(chunk));
        req.on('end', () => {
            receivedPngBuffer = Buffer.concat(chunks);
            res.writeHead(200, { 'Content-Type': 'text/plain', 'Access-Control-Allow-Origin': '*' });
            res.end('ok');
        });
        return;
    }
    let filePath = path.join(distDir, parsedUrl.pathname === '/' ? 'index.html' : parsedUrl.pathname);
    if (fs.existsSync(filePath) && fs.statSync(filePath).isFile()) {
        const ext = path.extname(filePath);
        const mime = {
            '.html': 'text/html',
            '.js': 'application/javascript',
            '.wasm': 'application/wasm',
            '.css': 'text/css',
            '.json': 'application/json',
            '.png': 'image/png',
            '.svg': 'image/svg+xml'
        }[ext] || 'application/octet-stream';
        res.writeHead(200, { 'Content-Type': mime, 'Access-Control-Allow-Origin': '*' });
        fs.createReadStream(filePath).pipe(res);
    } else {
        res.writeHead(404);
        res.end();
    }
});

await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;

const firefoxPath = process.env.FIREFOX_BIN || (fs.existsSync('/usr/bin/firefox') ? '/usr/bin/firefox' : 'firefox');
const browser = await puppeteer.launch({
    browser: 'firefox',
    executablePath: firefoxPath,
    headless: true,
    args: ['--no-sandbox'],
    extraPrefsFirefox: {
        'webgl.force-enabled': true,
        'webgl.disabled': false,
        'layers.acceleration.force-enabled': true
    }
});

try {
    const page = await browser.newPage();
    await page.evaluateOnNewDocument(() => {
        console.log = () => {};
        const origError = console.error;
        console.error = (...args) => {
            window.__lastError = args.map(a => {
                if (a instanceof Error) return a.stack || a.message;
                try { return JSON.stringify(a); } catch { return String(a); }
            }).join(' ');
            origError.apply(console, args);
        };
    });
    page.on('pageerror', err => console.error('PAGE ERROR:', err.toString()));
    await page.goto(`http://127.0.0.1:${port}/?source=local.host/bp.txt`, { waitUntil: 'load', timeout: 180000 });
    const status = await page.waitForFunction(() => {
        if (window.__lastError) return { error: window.__lastError };
        const el = document.getElementById('loadingScreen');
        if (!el || el.classList.contains('active') || !window.editor) return false;
        if (!window.editor.haveBlueprint()) return { ok: true };
        const bpc = window.__BPC;
        if (!bpc) return false;
        const stack = [bpc.entitySprites, bpc.tileSprites, bpc.overlayContainer];
        while (stack.length > 0) {
            const node = stack.pop();
            if (!node) continue;
            if (node.texture && node.texture.dynamic) return false;
            if (node.children) {
                for (let i = 0; i < node.children.length; i++) {
                    stack.push(node.children[i]);
                }
            }
        }
        return { ok: true };
    }, { timeout: 180000 }).then(h => h.jsonValue());
    if (status && status.error) {
        throw new Error(`FBE page error: ${status.error}`);
    }

    const exported = await page.evaluate(async (uploadUrl) => {
        const bpc = window.__BPC;
        if (bpc) {
            const stack = [bpc];
            while (stack.length > 0) {
                const node = stack.pop();
                if (!node) continue;
                if (typeof node.onViewUpdate === 'function') node.onViewUpdate();
                if (node.renderGroup) node.renderGroup.structureDidChange = true;
                if (node.children) {
                    for (let i = 0; i < node.children.length; i++) {
                        stack.push(node.children[i]);
                    }
                }
            }
        }
        const blob = await window.editor.getPicture();
        if (!blob) return false;
        const resp = await fetch(uploadUrl, { method: 'POST', body: blob });
        return resp.ok;
    }, `http://127.0.0.1:${port}/upload`);

    if (!exported || !receivedPngBuffer) {
        throw new Error('Failed to export blueprint picture: editor.getPicture() returned null');
    }

    fs.mkdirSync(path.dirname(path.resolve(outPath)), { recursive: true });
    fs.writeFileSync(outPath, receivedPngBuffer);
    console.log(`Rendered blueprint saved to ${outPath} (${fs.statSync(outPath).size} bytes)`);
} finally {
    await browser.close();
    server.close();
}
