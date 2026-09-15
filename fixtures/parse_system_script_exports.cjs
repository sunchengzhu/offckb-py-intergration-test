// Parse CLI exports through the installed public CCC SDK and JSON parser.
const fs = require("node:fs");
const { createRequire } = require("node:module");

async function main() {
  const [cliEntry, requestPath] = process.argv.slice(2);
  const ccc = createRequire(fs.realpathSync(cliEntry))("@ckb-ccc/core");
  const request = JSON.parse(fs.readFileSync(requestPath, "utf8"));
  const client = new ccc.ClientPublicTestnet({
    url: request.rpcUrl, fallbacks: [], scripts: request.ccc,
  });
  for (const script of Object.values(request.ccc)) {
    ccc.Script.from({ ...script, args: "0x" }).toBytes();
    for (const { cellDep } of script.cellDeps) ccc.CellDep.from(cellDep).toBytes();
  }
  const representatives = {};
  for (const [name, known] of [
    ["secp256k1_blake160_sighash_all", ccc.KnownScript.Secp256k1Blake160],
    ["xudt", ccc.KnownScript.XUdt],
  ]) {
    const resolved = await client.getKnownScript(known);
    if (resolved.codeHash !== request.ccc[known].codeHash) {
      throw new Error(`CCC did not use the exported ${name} reference`);
    }
    representatives[name] = request.ccc[known];
  }
  process.stdout.write(JSON.stringify({ representatives }) + "\n");
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
