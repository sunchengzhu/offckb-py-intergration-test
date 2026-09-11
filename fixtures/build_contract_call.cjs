// Prepare a real user transaction through the public CCC SDK. Python submits
// it through OffCKB's proxy so a rejection remains observable by the test.
const fs = require("node:fs");
const { createRequire } = require("node:module");

async function main() {
  const [cliEntry, requestPath, keyOption, keyPath] = process.argv.slice(2);
  if (keyOption !== "--privkey-file" || !keyPath) throw new Error("Expected --privkey-file");
  const load = createRequire(fs.realpathSync(cliEntry));
  const ccc = load("@ckb-ccc/core");
  const { cccA } = load("@ckb-ccc/core/advanced");
  const request = JSON.parse(fs.readFileSync(requestPath, "utf8"));
  const system = request.systemScripts.devnet;
  const client = new ccc.ClientPublicTestnet({
    url: request.rpcUrl,
    fallbacks: [],
    scripts: {
      [ccc.KnownScript.Secp256k1Blake160]: system.secp256k1_blake160_sighash_all.script,
      [ccc.KnownScript.Secp256k1Multisig]: system.secp256k1_blake160_multisig_all.script,
      [ccc.KnownScript.NervosDao]: system.dao.script,
      [ccc.KnownScript.AnyoneCanPay]: system.anyone_can_pay.script,
      [ccc.KnownScript.OmniLock]: system.omnilock.script,
      [ccc.KnownScript.XUdt]: system.xudt.script,
    },
  });
  const signer = new ccc.SignerCkbPrivateKey(client, fs.readFileSync(keyPath, "utf8").trim());
  const vm = system.ckb_js_vm.script;
  const contract = request.contract;
  const type = {
    codeHash: vm.codeHash,
    hashType: vm.hashType,
    args: "0x0000" + contract.codeHash.slice(2)
      + ccc.hexFrom(ccc.hashTypeToBytes(contract.hashType)).slice(2) + "00".repeat(32),
  };
  const tx = ccc.Transaction.from({
    outputs: [{ lock: (await signer.getRecommendedAddressObj()).script, type }],
    cellDeps: [...vm.cellDeps, ...contract.cellDeps].map(({ cellDep }) => cellDep),
  });
  await tx.completeInputsByCapacity(signer);
  await tx.completeFeeBy(signer, 1000);
  const signed = await signer.signTransaction(tx);
  const transaction = cccA.JsonRpcTransformers.transactionFrom(signed);
  for (const output of transaction.outputs) output.type ??= null;
  process.stdout.write(JSON.stringify({
    txHash: signed.hash(),
    transaction,
  }) + "\n");
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
