// A user's SDK transaction, using only references displayed by the installed CLI.
const fs = require("node:fs");
const { createRequire } = require("node:module");

async function main() {
  const [cliEntry, requestPath, keyOption, keyPath] = process.argv.slice(2);
  if (keyOption !== "--privkey-file" || !keyPath) throw new Error("Expected --privkey-file");
  const ccc = createRequire(fs.realpathSync(cliEntry))("@ckb-ccc/core");
  const request = JSON.parse(fs.readFileSync(requestPath, "utf8"));
  const exported = request.scripts.secp256k1_blake160_sighash_all;
  const client = new ccc.ClientPublicTestnet({
    url: request.rpcUrl,
    fallbacks: [],
    scripts: {
      [ccc.KnownScript.Secp256k1Blake160]: exported,
      [ccc.KnownScript.NervosDao]: request.scripts.dao,
      [ccc.KnownScript.AnyoneCanPay]: request.scripts.anyone_can_pay,
    },
  });
  const signer = new ccc.SignerCkbPrivateKey(client, fs.readFileSync(keyPath, "utf8").trim());
  const tx = ccc.Transaction.from({
    inputs: [{ previousOutput: { txHash: request.input.tx_hash, index: request.input.index }, since: 0 }],
    outputs: [{ lock: request.lock, capacity: BigInt(request.capacity) - 100000n }],
    outputsData: ["0x"],
    cellDeps: exported.cellDeps.map(({ cellDep }) => cellDep),
  });
  await tx.prepareSighashAllWitness(request.lock, 65, client);
  // Signing only: no automatic fee completion or replacement of the CLI's deps.
  const signed = await signer.signOnlyTransaction(tx);
  const txHash = await client.sendTransaction(signed);
  process.stdout.write(JSON.stringify({ txHash }) + "\n");
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
