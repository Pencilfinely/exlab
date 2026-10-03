/* Regenerate desktop and mobile icon variants from the approved SVG artwork. Requires sharp. */
'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const sharp = require('sharp');

const assets = path.resolve(__dirname, '../assets');
// Caption/tray (16 px) and taskbar (32 px) at Windows' 25% scale steps.
const sizes = [16, 20, 24, 28, 32, 36, 40, 44, 48, 56, 64, 72, 80, 88, 96, 112, 128, 256];

// Runtime sizes use 32-bit DIB for .NET Framework. The Windows shell's
// 256 px frame uses lossless PNG compression.
function dib(rgba, size) {
  const maskStride = Math.ceil(size / 32) * 4;
  const pixelsLength = size * size * 4;
  const result = Buffer.alloc(40 + pixelsLength + maskStride * size);
  result.writeUInt32LE(40, 0);
  result.writeInt32LE(size, 4);
  result.writeInt32LE(size * 2, 8);
  result.writeUInt16LE(1, 12);
  result.writeUInt16LE(32, 14);
  result.writeUInt32LE(pixelsLength + maskStride * size, 20);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const source = (y * size + x) * 4;
      const target = 40 + ((size - 1 - y) * size + x) * 4;
      result[target] = rgba[source + 2];
      result[target + 1] = rgba[source + 1];
      result[target + 2] = rgba[source];
      result[target + 3] = rgba[source + 3];
      if (rgba[source + 3] === 0) {
        result[40 + pixelsLength + (size - 1 - y) * maskStride + (x >> 3)] |= 0x80 >> (x & 7);
      }
    }
  }
  return result;
}

async function build(name) {
  const source = await fs.readFile(path.join(assets, `${name}.svg`));
  const frames = [];
  for (const size of sizes) {
    const png = await sharp(source, { density: 72 * size / 48 })
      .resize(size, size).ensureAlpha().png().toBuffer();
    await fs.writeFile(path.join(assets, 'generated', `${name}-${size}.png`), png);
    const data = size === 256 ? png : dib(await sharp(png).ensureAlpha().raw().toBuffer(), size);
    frames.push({ size, data });
  }
  const directory = Buffer.alloc(6 + 16 * frames.length);
  directory.writeUInt16LE(1, 2);
  directory.writeUInt16LE(frames.length, 4);
  let offset = directory.length;
  frames.forEach(({ size, data }, index) => {
    const entry = 6 + index * 16;
    directory[entry] = directory[entry + 1] = size === 256 ? 0 : size;
    directory.writeUInt16LE(1, entry + 4);
    directory.writeUInt16LE(32, entry + 6);
    directory.writeUInt32LE(data.length, entry + 8);
    directory.writeUInt32LE(offset, entry + 12);
    offset += data.length;
  });
  await fs.writeFile(path.join(assets, `${name}.ico`), Buffer.concat([directory, ...frames.map(frame => frame.data)]));
  console.log(`Built ${name}.ico (${sizes.join(', ')} px)`);
}

async function main() {
  await fs.mkdir(path.join(assets, 'generated'), { recursive: true });
  for (const name of ['center', 'worker', 'monitor']) await build(name);
  await fs.copyFile(path.join(assets, 'center.ico'), path.resolve(__dirname, '../expman/static/favicon.ico'));
  await mobileIcons();
  await fs.writeFile(path.join(assets, 'generated', 'renderer.json'), JSON.stringify({
    sharp: sharp.versions.sharp, rsvg: sharp.versions.rsvg, sizes,
  }, null, 2) + '\n');
}

async function mobileIcons() {
  const source = await fs.readFile(path.join(assets, 'monitor.svg'));
  const root = path.resolve(__dirname, '..');
  const resources = path.join(root, 'mobile/android/app/src/main/res');
  const web = path.join(root, 'expman/static/mobile');
  await fs.copyFile(path.join(assets, 'monitor.ico'), path.join(web, 'favicon.ico'));
  await fs.writeFile(path.join(web, 'icon.svg'), source);
  for (const size of [180, 192, 512]) {
    const png = await sharp(source, { density: 72 * size / 48 }).resize(size, size).png().toBuffer();
    await fs.writeFile(path.join(web, `icon-${size}.png`), png);
    await fs.writeFile(path.join(assets, 'generated', `monitor-${size}.png`), png);
  }
  for (const [density, size] of [['mdpi',48],['hdpi',72],['xhdpi',96],['xxhdpi',144],['xxxhdpi',192]]) {
    const folder = path.join(resources, 'mipmap-' + density); await fs.mkdir(folder, {recursive:true});
    const png = await sharp(source, {density:72 * size / 48}).resize(size,size).png().toBuffer();
    await fs.writeFile(path.join(folder,'ic_launcher.png'),png);
    const circle = Buffer.from(`<svg width="${size}" height="${size}" xmlns="http://www.w3.org/2000/svg"><circle cx="${size/2}" cy="${size/2}" r="${size/2}" fill="#FFB6C1"/></svg>`);
    const round = await sharp(circle).composite([{input:png},{input:circle,blend:'dest-in'}]).png().toBuffer();
    await fs.writeFile(path.join(folder,'ic_launcher_round.png'),round);
  }
  // The 48 px artwork fits inside Android's 66 px adaptive-icon safe zone.
  const tags = source.toString().match(/<path\b[^>]*>/g) || [];
  if (tags.length !== 5) throw new Error('Review the Monitor SVG paths before regenerating adaptive icons.');
  function color(value) {
    if (/^#[\da-f]{6}$/i.test(value)) return value;
    const rgba = value.match(/^rgba\((\d+),\s*(\d+),\s*(\d+),\s*1\)$/);
    if (!rgba) throw new Error('Unsupported Monitor SVG color: '+value);
    return '#' + rgba.slice(1).map(c=>Number(c).toString(16).padStart(2,'0')).join('');
  }
  function vector(monochrome) {
    const paths = tags.map(tag=>{
      const data=tag.match(/\bd="([^"]+)"/)[1],fill=tag.match(/\bfill="([^"]+)"/)[1];
      return `    <path android:fillColor="${monochrome?'#FFFFFFFF':color(fill)}" android:fillType="${tag.includes('fill-rule="evenodd"')?'evenOdd':'nonZero'}" android:pathData="${data}"/>`;
    }).join('\n');
    return `<?xml version="1.0" encoding="utf-8"?>\n<vector xmlns:android="http://schemas.android.com/apk/res/android" android:width="108dp" android:height="108dp" android:viewportWidth="108" android:viewportHeight="108">\n  <group android:scaleX="1.25" android:scaleY="1.25" android:translateX="24" android:translateY="24">\n${paths}\n  </group>\n</vector>\n`;
  }
  await fs.writeFile(path.join(resources,'drawable/monitor_foreground.xml'),vector(false));
  await fs.writeFile(path.join(resources,'drawable/monitor_monochrome.xml'),vector(true));
  for (const qualifier of ['', '-v33']) {
    const folder=path.join(resources,'mipmap-anydpi'+qualifier);await fs.mkdir(folder,{recursive:true});
    const xml=`<?xml version="1.0" encoding="utf-8"?>\n<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n  <background android:drawable="@color/monitor_icon_background"/>\n  <foreground android:drawable="@drawable/monitor_foreground"/>\n  <monochrome android:drawable="@drawable/monitor_monochrome"/>\n</adaptive-icon>\n`;
    for(const name of ['ic_launcher','ic_launcher_round'])await fs.writeFile(path.join(folder,name+'.xml'),xml);
  }
  await fs.writeFile(path.join(resources,'values/icon_colors.xml'),'<?xml version="1.0" encoding="utf-8"?>\n<resources><color name="monitor_icon_background">#FFB6C1</color></resources>\n');
  await fs.writeFile(path.join(root,'mobile/harmonyos/AppScope/resources/base/media/app_icon.svg'),source);
  console.log('Built Monitor launcher densities, adaptive / round / monochrome icons, web and HarmonyOS artwork.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
