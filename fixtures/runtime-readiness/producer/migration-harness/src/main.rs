//! The contract-2 publisher (saas-fabric #139) writing its first generation,
//! so the contract-3 harness can publish over it.
//!
//!     cargo run -- <state-directory>

use std::path::PathBuf;

use fabric_runtime_publication::{
    CatalogDocument, DataSourceDocument, DocumentInput, DocumentRevision, FilesystemRuntimePublication,
    RuntimePublication as _, RuntimeSnapshot, TenantBindingDocument,
};
use serde_json::json;

#[tokio::main]
async fn main() {
    let state = PathBuf::from(std::env::args().nth(1).expect("usage: migration-harness <state-directory>"));
    for document in ["tenants", "data-sources", "catalog"] {
        std::fs::create_dir_all(state.join(document)).unwrap();
    }
    let tenants: Vec<TenantBindingDocument> = serde_json::from_value(json!([{
        "tenant": "alpha",
        "revision": 1,
        "data": {"primary": {"data_source": "alpha-primary", "isolation": {"kind": "database"}}},
    }]))
    .unwrap();
    let data_sources: Vec<DataSourceDocument> = serde_json::from_value(json!([{
        "id": "alpha-primary",
        "revision": 1,
        "connector": "postgres-alpha",
        "connection": {"kind": "named", "name": "alpha-primary"},
        "placement": "dedicated",
        "residency": {"region": "nz-north", "jurisdiction": "NZ"},
    }]))
    .unwrap();
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
    FilesystemRuntimePublication::new(
        state.join("tenants/tenants.json"),
        state.join("data-sources/data-sources.json"),
        state.join("catalog/catalog.json"),
    )
    .publish(&RuntimeSnapshot {
        tenants: DocumentInput::new(DocumentRevision::new(1), tenants),
        data_sources: DocumentInput::new(DocumentRevision::new(1), data_sources),
        catalog: DocumentInput::new(DocumentRevision::new(1), catalog),
    })
    .await
    .unwrap();
}
