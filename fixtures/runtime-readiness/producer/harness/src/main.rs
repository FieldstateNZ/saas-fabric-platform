//! Publishes synthetic generations with saas-fabric's own
//! `FilesystemRuntimePublication` and copies every published file out.
//!
//!     cargo run -- fresh <out-directory>
//!         alpha alone (registry generation 1) into <out>/held/, then alpha
//!         and beta (registry generation 2) into <out>/published/.
//!
//!     cargo run -- over <state-directory> <out-directory>
//!         publishes alpha and beta over the files an earlier publisher left
//!         in <state-directory> (see ../migration-harness), copying the state
//!         before into <out>/held/ and after into <out>/published/.
//!
//! The platform's checker then reads these exact bytes
//! (scripts/test_runtime_contract.py, `ProducerToChecker`).

use std::path::{Path, PathBuf};

use fabric_core::TenantId;
use fabric_runtime_publication::{
    CatalogDocument, DataSourceDocument, DocumentInput, DocumentRevision, FilesystemRuntimePublication,
    IssuerRegistration, RegistryIssuer, RuntimePublication as _, RuntimeSnapshot, TenantBindingDocument,
};
use serde_json::json;

const DOCUMENTS: [&str; 3] = ["tenants", "data-sources", "catalog"];

fn tenant(id: &str, revision: u64) -> serde_json::Value {
    json!({
        "tenant": id,
        "revision": revision,
        "data": {"primary": {"data_source": format!("{id}-primary"), "isolation": {"kind": "database"}}},
    })
}

fn data_source(id: &str, revision: u64) -> serde_json::Value {
    json!({
        "id": format!("{id}-primary"),
        "revision": revision,
        "connector": format!("postgres-{id}"),
        "connection": {"kind": "named", "name": format!("{id}-primary")},
        "placement": "dedicated",
        "residency": {"region": "nz-north", "jurisdiction": "NZ"},
    })
}

fn registered(id: &str) -> IssuerRegistration {
    IssuerRegistration {
        issuer: RegistryIssuer::parse(&format!("https://identity.example.invalid/realms/{id}")).unwrap(),
        tenant: TenantId::try_new(id).unwrap(),
    }
}

fn snapshot(revision: u64, catalog_revision: u64, clients: &[(&str, u64)], registry: &[&str]) -> RuntimeSnapshot {
    let tenants: Vec<TenantBindingDocument> =
        serde_json::from_value(clients.iter().map(|(id, rev)| tenant(id, *rev)).collect()).unwrap();
    let data_sources: Vec<DataSourceDocument> =
        serde_json::from_value(clients.iter().map(|(id, rev)| data_source(id, *rev)).collect()).unwrap();
    let catalog: CatalogDocument = serde_json::from_value(json!({
        "notes": {
            "data_source": "primary",
            "collection": "notes",
            "key_field": "id",
            "operations": ["read", "list", "create", "update", "delete"],
            "queryable_fields": ["id", "title"],
        }
    }))
    .unwrap();
    RuntimeSnapshot {
        tenants: DocumentInput::new(DocumentRevision::new(revision), tenants),
        data_sources: DocumentInput::new(DocumentRevision::new(revision), data_sources),
        catalog: DocumentInput::new(DocumentRevision::new(catalog_revision), catalog),
        registry: Some(registry.iter().copied().map(registered).collect()),
    }
}

fn copy_out(state: &Path, out: &Path) {
    for document in DOCUMENTS {
        let from = state.join(document);
        let to = out.join(document);
        std::fs::create_dir_all(&to).unwrap();
        for entry in std::fs::read_dir(&from).unwrap() {
            let entry = entry.unwrap();
            let name = entry.file_name();
            if name.to_string_lossy().starts_with('.') {
                continue;
            }
            std::fs::copy(entry.path(), to.join(name)).unwrap();
        }
    }
}

fn publisher(state: &Path) -> FilesystemRuntimePublication {
    for document in DOCUMENTS {
        std::fs::create_dir_all(state.join(document)).unwrap();
    }
    FilesystemRuntimePublication::new(
        state.join("tenants/tenants.json"),
        state.join("data-sources/data-sources.json"),
        state.join("catalog/catalog.json"),
    )
}

/// The second generation: alpha advanced, beta added, the catalogue left
/// unchanged so its manifest keeps the contract it was first written at.
fn second() -> RuntimeSnapshot {
    snapshot(2, 1, &[("alpha", 2), ("beta", 1)], &["beta", "alpha"])
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("fresh") => {
            let out = PathBuf::from(&args[2]);
            let state = std::env::temp_dir().join(format!("producer-state-{}", std::process::id()));
            let publisher = publisher(&state);
            publisher.publish(&snapshot(1, 1, &[("alpha", 1)], &["alpha"])).await.unwrap();
            copy_out(&state, &out.join("held"));
            publisher.publish(&second()).await.unwrap();
            copy_out(&state, &out.join("published"));
            std::fs::remove_dir_all(&state).unwrap();
        }
        Some("over") => {
            let state = PathBuf::from(&args[2]);
            let out = PathBuf::from(&args[3]);
            copy_out(&state, &out.join("held"));
            publisher(&state).publish(&second()).await.unwrap();
            copy_out(&state, &out.join("published"));
        }
        _ => panic!("usage: harness fresh <out> | harness over <state> <out>"),
    }
}
