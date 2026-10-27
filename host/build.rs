// build.rs
// copy ../supervisor/* -> target/..../_py
use std::{
    collections::BTreeSet, env, fmt::Debug, fs, io, path::{Path, PathBuf},
};

fn copy_dir_all(src: impl AsRef<Path>
    , dst: impl AsRef<Path>+Debug) -> io::Result<()> {
    fs::create_dir_all(&dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let ty = entry.file_type()?;
        if ty.is_dir() {
            copy_dir_all(entry.path(), dst.as_ref().join(entry.file_name()))?;
        } else {
            println!("cargo:rerun-if-changed={}",entry.path().to_string_lossy());
            fs::copy(entry.path(), dst.as_ref().join(entry.file_name()))?;
        }
    }
    Ok(())
}
fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    let manifest_dir =PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap()) ;
    
    let dest_root = exe_dir().join("_py");

    fs::create_dir_all(&dest_root).expect("create _py dir");
    
    copy_dir_all(manifest_dir.parent().unwrap().join("supervisor"),dest_root).unwrap();
}

fn exe_dir() -> PathBuf {
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("OUT_DIR"));
    out.ancestors().nth(3).expect("locate profile dir").to_path_buf()
}
