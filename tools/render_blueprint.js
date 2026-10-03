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

const server = http.createServer({ maxHeaderSize: 32 * 1024 * 1024 }, (req, res) => {
    const parsedUrl = new URL(req.url, 'http://127.0.0.1');
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
    page.on('console', msg => console.error('PAGE LOG:', msg.text()));
    page.on('pageerror', err => console.error('PAGE ERROR:', err.toString()));
    await page.goto(`http://127.0.0.1:${port}/?source=${encodeURIComponent(bpStr)}`, { waitUntil: 'load' });
    await page.waitForFunction(() => {
        const el = document.getElementById('loadingScreen');
        return el && !el.classList.contains('active') && window.editor;
    }, { timeout: 60000 });

    await new Promise(r => setTimeout(r, 1000));

    const dataUrl = await page.evaluate(async () => {
        const blob = await window.editor.getPicture();
        if (!blob) return null;
        return new Promise((resolve) => {
            const reader = new FileReader();
            reader.onloadend = () => resolve(reader.result);
            reader.readAsDataURL(blob);
        });
    });

    if (!dataUrl) {
        throw new Error('Failed to export blueprint picture: editor.getPicture() returned null');
    }

    const base64Data = dataUrl.replace(/^data:image\/png;base64,/, '');
    fs.mkdirSync(path.dirname(path.resolve(outPath)), { recursive: true });
    fs.writeFileSync(outPath, Buffer.from(base64Data, 'base64'));
    console.log(`Rendered blueprint saved to ${outPath} (${fs.statSync(outPath).size} bytes)`);
} finally {
    await browser.close();
    server.close();
}
