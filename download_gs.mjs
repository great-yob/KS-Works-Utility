import fs from 'fs';
import path from 'path';
import { Readable } from 'stream';
import { finished } from 'stream/promises';
import { execSync } from 'child_process';
import sevenBin from '7zip-bin';

const gsUrl = 'https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10031/gs10031w64.exe';
const installerPath = path.join(process.cwd(), 'gs_installer.exe');
const extractDir = path.join(process.cwd(), 'resources', 'ghostscript');

async function downloadAndExtract() {
  console.log('Downloading Ghostscript...');
  const response = await fetch(gsUrl);
  if (!response.ok) throw new Error(`Failed to download: ${response.statusText}`);
  const fileStream = fs.createWriteStream(installerPath);
  await finished(Readable.fromWeb(response.body).pipe(fileStream));
  console.log('Download complete.');

  console.log('Extracting with 7zip...');
  if (fs.existsSync(extractDir)) {
    fs.rmSync(extractDir, { recursive: true, force: true });
  }
  fs.mkdirSync(extractDir, { recursive: true });

  const sevenZipPath = sevenBin.path7za;
  console.log(`Using 7z at: ${sevenZipPath}`);
  
  // Extract NSIS installer
  execSync(`"${sevenZipPath}" x "${installerPath}" -o"${extractDir}" -y`, { stdio: 'inherit' });
  
  // Rename $_OUTDIR to gs
  // Typically NSIS extracts to $_OUTDIR or similar folders
  console.log('Extraction complete! Files in resources/ghostscript.');
  fs.unlinkSync(installerPath);
}

downloadAndExtract().catch(console.error);
