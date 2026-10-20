import { copyFile, constants } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const projectRoot = new URL('../', import.meta.url);
try {
  await copyFile(
    fileURLToPath(new URL('.dev.vars.example', projectRoot)),
    fileURLToPath(new URL('.dev.vars', projectRoot)),
    constants.COPYFILE_EXCL,
  );
  console.log('Lokalna demo konfiguracija je pripremljena.');
} catch (error) {
  if (error.code !== 'EEXIST') throw error;
  console.log('Postojeća lokalna konfiguracija je sačuvana.');
}
