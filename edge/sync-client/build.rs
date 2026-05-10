use std::path::PathBuf;

fn main() {
    // Compile the canonical proto from infra/proto/ into Rust types.
    // The proto file lives at the repo root so master + edge stay in lockstep.
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let workspace_root = manifest_dir
        .parent()
        .and_then(|p| p.parent())
        .expect("workspace root is the grand-parent of sync-client/Cargo.toml");

    let proto = workspace_root.join("infra/proto/lucifer_sync.proto");
    let proto_dir = workspace_root.join("infra/proto");

    println!("cargo:rerun-if-changed={}", proto.display());

    // Bundle protoc so contributors don't need a system install.
    let protoc = protoc_bin_vendored::protoc_bin_path().expect("vendored protoc binary not found");
    std::env::set_var("PROTOC", protoc);

    tonic_build::configure()
        .build_server(false)
        .build_client(true)
        .compile_protos(&[proto], &[proto_dir])
        .expect("failed to compile lucifer_sync.proto");
}
