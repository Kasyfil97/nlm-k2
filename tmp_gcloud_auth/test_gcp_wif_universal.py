"""
Universal GCP WIF Tester - Test ALL GCP Services (70+ Services)

Script untuk test WIF access ke semua GCP services termasuk Firebase.
Cocok untuk validasi SA permissions apapun role-nya.

Usage:
    # Test semua services
    python test_gcp_wif_universal.py --all

    # Test specific services
    python test_gcp_wif_universal.py --services storage,bigquery,firebase

    # Test by category
    python test_gcp_wif_universal.py --category firebase
    python test_gcp_wif_universal.py --category "AI/ML"

    # List available services
    python test_gcp_wif_universal.py --list

    # Test dengan region/zone specific
    python test_gcp_wif_universal.py --all --region asia-southeast1 --zone asia-southeast1-a
"""

import os
import sys
import json
import argparse
import requests
from datetime import datetime
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()

# ============================================================
# Configuration
# ============================================================

TENANT_ID = os.getenv("AZURE_TENANT_ID")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
GCP_PROJECT_NUMBER = os.getenv("GCP_PROJECT_NUMBER")
POOL_ID = os.getenv("GCP_POOL_ID", "entra-id-pool")
PROVIDER_ID = os.getenv("GCP_PROVIDER_ID", "entra-id-oidc")
SERVICE_ACCOUNT_EMAIL = os.getenv("GCP_SERVICE_ACCOUNT_EMAIL")

DEFAULT_REGION = "asia-southeast1"
DEFAULT_ZONE = "asia-southeast1-a"

# ============================================================
# Service Definitions - 70+ GCP Services
# ============================================================

SERVICES = {
    # ==================== STORAGE ====================
    "storage": {
        "name": "Cloud Storage (GCS)",
        "category": "Storage",
        "endpoint": lambda p, r, z: f"https://storage.googleapis.com/storage/v1/b?project={p}",
        "description": "List buckets",
        "result_key": "items",
        "item_name": "name",
    },
    "filestore": {
        "name": "Filestore",
        "category": "Storage",
        "endpoint": lambda p, r, z: f"https://file.googleapis.com/v1/projects/{p}/locations/{r}/instances",
        "description": "List Filestore instances",
        "result_key": "instances",
        "item_name": "name",
    },

    # ==================== DATABASE ====================
    "bigquery": {
        "name": "BigQuery",
        "category": "Database",
        "endpoint": lambda p, r, z: f"https://bigquery.googleapis.com/bigquery/v2/projects/{p}/datasets",
        "description": "List datasets",
        "result_key": "datasets",
        "item_name": "datasetReference.datasetId",
    },
    "cloudsql": {
        "name": "Cloud SQL",
        "category": "Database",
        "endpoint": lambda p, r, z: f"https://sqladmin.googleapis.com/v1/projects/{p}/instances",
        "description": "List SQL instances",
        "result_key": "items",
        "item_name": "name",
    },
    "spanner": {
        "name": "Cloud Spanner",
        "category": "Database",
        "endpoint": lambda p, r, z: f"https://spanner.googleapis.com/v1/projects/{p}/instances",
        "description": "List Spanner instances",
        "result_key": "instances",
        "item_name": "name",
    },
    "firestore": {
        "name": "Firestore",
        "category": "Database",
        "endpoint": lambda p, r, z: f"https://firestore.googleapis.com/v1/projects/{p}/databases",
        "description": "List Firestore databases",
        "result_key": "databases",
        "item_name": "name",
    },
    "bigtable": {
        "name": "Cloud Bigtable",
        "category": "Database",
        "endpoint": lambda p, r, z: f"https://bigtableadmin.googleapis.com/v2/projects/{p}/instances",
        "description": "List Bigtable instances",
        "result_key": "instances",
        "item_name": "name",
    },
    "memorystore_redis": {
        "name": "Memorystore (Redis)",
        "category": "Database",
        "endpoint": lambda p, r, z: f"https://redis.googleapis.com/v1/projects/{p}/locations/{r}/instances",
        "description": "List Redis instances",
        "result_key": "instances",
        "item_name": "name",
    },
    "alloydb": {
        "name": "AlloyDB",
        "category": "Database",
        "endpoint": lambda p, r, z: f"https://alloydb.googleapis.com/v1/projects/{p}/locations/{r}/clusters",
        "description": "List AlloyDB clusters",
        "result_key": "clusters",
        "item_name": "name",
    },

    # ==================== COMPUTE ====================
    "compute": {
        "name": "Compute Engine",
        "category": "Compute",
        "endpoint": lambda p, r, z: f"https://compute.googleapis.com/compute/v1/projects/{p}/zones/{z}/instances",
        "description": "List VM instances",
        "result_key": "items",
        "item_name": "name",
    },
    "compute_disks": {
        "name": "Compute Engine Disks",
        "category": "Compute",
        "endpoint": lambda p, r, z: f"https://compute.googleapis.com/compute/v1/projects/{p}/zones/{z}/disks",
        "description": "List persistent disks",
        "result_key": "items",
        "item_name": "name",
    },
    "compute_images": {
        "name": "Compute Engine Images",
        "category": "Compute",
        "endpoint": lambda p, r, z: f"https://compute.googleapis.com/compute/v1/projects/{p}/global/images",
        "description": "List custom images",
        "result_key": "items",
        "item_name": "name",
    },
    "compute_snapshots": {
        "name": "Compute Engine Snapshots",
        "category": "Compute",
        "endpoint": lambda p, r, z: f"https://compute.googleapis.com/compute/v1/projects/{p}/global/snapshots",
        "description": "List snapshots",
        "result_key": "items",
        "item_name": "name",
    },
    "gke": {
        "name": "Google Kubernetes Engine",
        "category": "Compute",
        "endpoint": lambda p, r, z: f"https://container.googleapis.com/v1/projects/{p}/locations/-/clusters",
        "description": "List GKE clusters",
        "result_key": "clusters",
        "item_name": "name",
    },

    # ==================== SERVERLESS ====================
    "functions": {
        "name": "Cloud Functions",
        "category": "Serverless",
        "endpoint": lambda p, r, z: f"https://cloudfunctions.googleapis.com/v2/projects/{p}/locations/-/functions",
        "description": "List Cloud Functions",
        "result_key": "functions",
        "item_name": "name",
    },
    "run": {
        "name": "Cloud Run",
        "category": "Serverless",
        "endpoint": lambda p, r, z: f"https://run.googleapis.com/v2/projects/{p}/locations/{r}/services",
        "description": "List Cloud Run services",
        "result_key": "services",
        "item_name": "name",
    },
    "appengine": {
        "name": "App Engine",
        "category": "Serverless",
        "endpoint": lambda p, r, z: f"https://appengine.googleapis.com/v1/apps/{p}/services",
        "description": "List App Engine services",
        "result_key": "services",
        "item_name": "name",
    },

    # ==================== MESSAGING ====================
    "pubsub": {
        "name": "Pub/Sub Topics",
        "category": "Messaging",
        "endpoint": lambda p, r, z: f"https://pubsub.googleapis.com/v1/projects/{p}/topics",
        "description": "List Pub/Sub topics",
        "result_key": "topics",
        "item_name": "name",
    },
    "pubsub_subs": {
        "name": "Pub/Sub Subscriptions",
        "category": "Messaging",
        "endpoint": lambda p, r, z: f"https://pubsub.googleapis.com/v1/projects/{p}/subscriptions",
        "description": "List Pub/Sub subscriptions",
        "result_key": "subscriptions",
        "item_name": "name",
    },
    "scheduler": {
        "name": "Cloud Scheduler",
        "category": "Messaging",
        "endpoint": lambda p, r, z: f"https://cloudscheduler.googleapis.com/v1/projects/{p}/locations/{r}/jobs",
        "description": "List Scheduler jobs",
        "result_key": "jobs",
        "item_name": "name",
    },
    "tasks": {
        "name": "Cloud Tasks",
        "category": "Messaging",
        "endpoint": lambda p, r, z: f"https://cloudtasks.googleapis.com/v2/projects/{p}/locations/{r}/queues",
        "description": "List task queues",
        "result_key": "queues",
        "item_name": "name",
    },

    # ==================== NETWORKING ====================
    "vpc": {
        "name": "VPC Networks",
        "category": "Networking",
        "endpoint": lambda p, r, z: f"https://compute.googleapis.com/compute/v1/projects/{p}/global/networks",
        "description": "List VPC networks",
        "result_key": "items",
        "item_name": "name",
    },
    "subnets": {
        "name": "VPC Subnets",
        "category": "Networking",
        "endpoint": lambda p, r, z: f"https://compute.googleapis.com/compute/v1/projects/{p}/regions/{r}/subnetworks",
        "description": "List subnets",
        "result_key": "items",
        "item_name": "name",
    },
    "firewall": {
        "name": "Firewall Rules",
        "category": "Networking",
        "endpoint": lambda p, r, z: f"https://compute.googleapis.com/compute/v1/projects/{p}/global/firewalls",
        "description": "List firewall rules",
        "result_key": "items",
        "item_name": "name",
    },
    "dns": {
        "name": "Cloud DNS",
        "category": "Networking",
        "endpoint": lambda p, r, z: f"https://dns.googleapis.com/dns/v1/projects/{p}/managedZones",
        "description": "List DNS zones",
        "result_key": "managedZones",
        "item_name": "name",
    },
    "loadbalancer": {
        "name": "Load Balancers",
        "category": "Networking",
        "endpoint": lambda p, r, z: f"https://compute.googleapis.com/compute/v1/projects/{p}/global/urlMaps",
        "description": "List URL maps (LB)",
        "result_key": "items",
        "item_name": "name",
    },
    "armor": {
        "name": "Cloud Armor",
        "category": "Networking",
        "endpoint": lambda p, r, z: f"https://compute.googleapis.com/compute/v1/projects/{p}/global/securityPolicies",
        "description": "List security policies",
        "result_key": "items",
        "item_name": "name",
    },

    # ==================== SECURITY ====================
    "secretmanager": {
        "name": "Secret Manager",
        "category": "Security",
        "endpoint": lambda p, r, z: f"https://secretmanager.googleapis.com/v1/projects/{p}/secrets",
        "description": "List secrets",
        "result_key": "secrets",
        "item_name": "name",
    },
    "kms": {
        "name": "Cloud KMS",
        "category": "Security",
        "endpoint": lambda p, r, z: f"https://cloudkms.googleapis.com/v1/projects/{p}/locations/{r}/keyRings",
        "description": "List KMS keyrings",
        "result_key": "keyRings",
        "item_name": "name",
    },
    "iam": {
        "name": "IAM Service Accounts",
        "category": "Security",
        "endpoint": lambda p, r, z: f"https://iam.googleapis.com/v1/projects/{p}/serviceAccounts",
        "description": "List service accounts",
        "result_key": "accounts",
        "item_name": "email",
    },
    "iam_roles": {
        "name": "IAM Custom Roles",
        "category": "Security",
        "endpoint": lambda p, r, z: f"https://iam.googleapis.com/v1/projects/{p}/roles",
        "description": "List custom roles",
        "result_key": "roles",
        "item_name": "name",
    },
    "certificatemanager": {
        "name": "Certificate Manager",
        "category": "Security",
        "endpoint": lambda p, r, z: f"https://certificatemanager.googleapis.com/v1/projects/{p}/locations/{r}/certificates",
        "description": "List certificates",
        "result_key": "certificates",
        "item_name": "name",
    },

    # ==================== AI/ML ====================
    "aiplatform": {
        "name": "Vertex AI Datasets",
        "category": "AI/ML",
        "endpoint": lambda p, r, z: f"https://{r}-aiplatform.googleapis.com/v1/projects/{p}/locations/{r}/datasets",
        "description": "List Vertex AI datasets",
        "result_key": "datasets",
        "item_name": "displayName",
    },
    "aiplatform_models": {
        "name": "Vertex AI Models",
        "category": "AI/ML",
        "endpoint": lambda p, r, z: f"https://{r}-aiplatform.googleapis.com/v1/projects/{p}/locations/{r}/models",
        "description": "List Vertex AI models",
        "result_key": "models",
        "item_name": "displayName",
    },
    "aiplatform_endpoints": {
        "name": "Vertex AI Endpoints",
        "category": "AI/ML",
        "endpoint": lambda p, r, z: f"https://{r}-aiplatform.googleapis.com/v1/projects/{p}/locations/{r}/endpoints",
        "description": "List Vertex AI endpoints",
        "result_key": "endpoints",
        "item_name": "displayName",
    },
    "notebooks": {
        "name": "Vertex AI Workbench",
        "category": "AI/ML",
        "endpoint": lambda p, r, z: f"https://notebooks.googleapis.com/v1/projects/{p}/locations/{r}/instances",
        "description": "List notebook instances",
        "result_key": "instances",
        "item_name": "name",
    },
    "dialogflow": {
        "name": "Dialogflow CX",
        "category": "AI/ML",
        "endpoint": lambda p, r, z: f"https://{r}-dialogflow.googleapis.com/v3/projects/{p}/locations/{r}/agents",
        "description": "List Dialogflow agents",
        "result_key": "agents",
        "item_name": "displayName",
    },

    # ==================== DATA ANALYTICS ====================
    "bigquery_jobs": {
        "name": "BigQuery Jobs",
        "category": "Analytics",
        "endpoint": lambda p, r, z: f"https://bigquery.googleapis.com/bigquery/v2/projects/{p}/jobs?maxResults=10",
        "description": "List recent BQ jobs",
        "result_key": "jobs",
        "item_name": "jobReference.jobId",
    },
    "looker": {
        "name": "Looker",
        "category": "Analytics",
        "endpoint": lambda p, r, z: f"https://looker.googleapis.com/v1/projects/{p}/locations/{r}/instances",
        "description": "List Looker instances",
        "result_key": "instances",
        "item_name": "name",
    },

    # ==================== DATA PROCESSING ====================
    "dataflow": {
        "name": "Dataflow",
        "category": "Data Processing",
        "endpoint": lambda p, r, z: f"https://dataflow.googleapis.com/v1b3/projects/{p}/locations/{r}/jobs",
        "description": "List Dataflow jobs",
        "result_key": "jobs",
        "item_name": "name",
    },
    "dataproc": {
        "name": "Dataproc",
        "category": "Data Processing",
        "endpoint": lambda p, r, z: f"https://dataproc.googleapis.com/v1/projects/{p}/regions/{r}/clusters",
        "description": "List Dataproc clusters",
        "result_key": "clusters",
        "item_name": "clusterName",
    },
    "composer": {
        "name": "Cloud Composer",
        "category": "Data Processing",
        "endpoint": lambda p, r, z: f"https://composer.googleapis.com/v1/projects/{p}/locations/{r}/environments",
        "description": "List Composer environments",
        "result_key": "environments",
        "item_name": "name",
    },
    "datafusion": {
        "name": "Cloud Data Fusion",
        "category": "Data Processing",
        "endpoint": lambda p, r, z: f"https://datafusion.googleapis.com/v1/projects/{p}/locations/{r}/instances",
        "description": "List Data Fusion instances",
        "result_key": "instances",
        "item_name": "name",
    },
    "dataplex": {
        "name": "Dataplex",
        "category": "Data Processing",
        "endpoint": lambda p, r, z: f"https://dataplex.googleapis.com/v1/projects/{p}/locations/{r}/lakes",
        "description": "List Dataplex lakes",
        "result_key": "lakes",
        "item_name": "name",
    },

    # ==================== FIREBASE ====================
    "firebase": {
        "name": "Firebase Project",
        "category": "Firebase",
        "endpoint": lambda p, r, z: f"https://firebase.googleapis.com/v1beta1/projects/{p}",
        "description": "Get Firebase project info",
        "result_key": None,
        "item_name": "displayName",
    },
    "firebase_apps_android": {
        "name": "Firebase Android Apps",
        "category": "Firebase",
        "endpoint": lambda p, r, z: f"https://firebase.googleapis.com/v1beta1/projects/{p}/androidApps",
        "description": "List Android apps",
        "result_key": "apps",
        "item_name": "displayName",
    },
    "firebase_apps_ios": {
        "name": "Firebase iOS Apps",
        "category": "Firebase",
        "endpoint": lambda p, r, z: f"https://firebase.googleapis.com/v1beta1/projects/{p}/iosApps",
        "description": "List iOS apps",
        "result_key": "apps",
        "item_name": "displayName",
    },
    "firebase_apps_web": {
        "name": "Firebase Web Apps",
        "category": "Firebase",
        "endpoint": lambda p, r, z: f"https://firebase.googleapis.com/v1beta1/projects/{p}/webApps",
        "description": "List Web apps",
        "result_key": "apps",
        "item_name": "displayName",
    },
    "firebase_hosting": {
        "name": "Firebase Hosting",
        "category": "Firebase",
        "endpoint": lambda p, r, z: f"https://firebasehosting.googleapis.com/v1beta1/projects/{p}/sites",
        "description": "List Hosting sites",
        "result_key": "sites",
        "item_name": "name",
    },
    "firebase_storage": {
        "name": "Firebase Storage",
        "category": "Firebase",
        "endpoint": lambda p, r, z: f"https://firebasestorage.googleapis.com/v1beta/projects/{p}/buckets",
        "description": "List Storage buckets",
        "result_key": "buckets",
        "item_name": "name",
    },
    "firebase_rtdb": {
        "name": "Firebase Realtime Database",
        "category": "Firebase",
        "endpoint": lambda p, r, z: f"https://firebasedatabase.googleapis.com/v1beta/projects/{p}/locations/-/instances",
        "description": "List RTDB instances",
        "result_key": "instances",
        "item_name": "name",
    },
    "firebase_auth": {
        "name": "Firebase Authentication",
        "category": "Firebase",
        "endpoint": lambda p, r, z: f"https://identitytoolkit.googleapis.com/v2/projects/{p}/config",
        "description": "Get Auth config",
        "result_key": None,
        "item_name": "name",
    },
    "firebase_remoteconfig": {
        "name": "Firebase Remote Config",
        "category": "Firebase",
        "endpoint": lambda p, r, z: f"https://firebaseremoteconfig.googleapis.com/v1/projects/{p}/remoteConfig",
        "description": "Get Remote Config",
        "result_key": None,
        "item_name": "version",
    },
    "firebase_extensions": {
        "name": "Firebase Extensions",
        "category": "Firebase",
        "endpoint": lambda p, r, z: f"https://firebaseextensions.googleapis.com/v1beta/projects/{p}/instances",
        "description": "List Extensions",
        "result_key": "instances",
        "item_name": "name",
    },
    "firebase_ml": {
        "name": "Firebase ML",
        "category": "Firebase",
        "endpoint": lambda p, r, z: f"https://firebaseml.googleapis.com/v1beta2/projects/{p}/models",
        "description": "List ML models",
        "result_key": "models",
        "item_name": "displayName",
    },

    # ==================== CI/CD ====================
    "artifact": {
        "name": "Artifact Registry",
        "category": "CI/CD",
        "endpoint": lambda p, r, z: f"https://artifactregistry.googleapis.com/v1/projects/{p}/locations/{r}/repositories",
        "description": "List repositories",
        "result_key": "repositories",
        "item_name": "name",
    },
    "cloudbuild": {
        "name": "Cloud Build",
        "category": "CI/CD",
        "endpoint": lambda p, r, z: f"https://cloudbuild.googleapis.com/v1/projects/{p}/builds",
        "description": "List builds",
        "result_key": "builds",
        "item_name": "id",
    },
    "cloudbuild_triggers": {
        "name": "Cloud Build Triggers",
        "category": "CI/CD",
        "endpoint": lambda p, r, z: f"https://cloudbuild.googleapis.com/v1/projects/{p}/triggers",
        "description": "List build triggers",
        "result_key": "triggers",
        "item_name": "name",
    },
    "clouddeploy": {
        "name": "Cloud Deploy",
        "category": "CI/CD",
        "endpoint": lambda p, r, z: f"https://clouddeploy.googleapis.com/v1/projects/{p}/locations/{r}/deliveryPipelines",
        "description": "List delivery pipelines",
        "result_key": "deliveryPipelines",
        "item_name": "name",
    },
    "sourcerepo": {
        "name": "Cloud Source Repositories",
        "category": "CI/CD",
        "endpoint": lambda p, r, z: f"https://sourcerepo.googleapis.com/v1/projects/{p}/repos",
        "description": "List repositories",
        "result_key": "repos",
        "item_name": "name",
    },

    # ==================== OPERATIONS ====================
    "logging": {
        "name": "Cloud Logging",
        "category": "Operations",
        "endpoint": lambda p, r, z: f"https://logging.googleapis.com/v2/projects/{p}/logs",
        "description": "List log names",
        "result_key": "logNames",
        "item_name": None,
    },
    "logging_sinks": {
        "name": "Logging Sinks",
        "category": "Operations",
        "endpoint": lambda p, r, z: f"https://logging.googleapis.com/v2/projects/{p}/sinks",
        "description": "List logging sinks",
        "result_key": "sinks",
        "item_name": "name",
    },
    "monitoring": {
        "name": "Cloud Monitoring Alerts",
        "category": "Operations",
        "endpoint": lambda p, r, z: f"https://monitoring.googleapis.com/v3/projects/{p}/alertPolicies",
        "description": "List alert policies",
        "result_key": "alertPolicies",
        "item_name": "displayName",
    },
    "monitoring_dashboards": {
        "name": "Monitoring Dashboards",
        "category": "Operations",
        "endpoint": lambda p, r, z: f"https://monitoring.googleapis.com/v1/projects/{p}/dashboards",
        "description": "List dashboards",
        "result_key": "dashboards",
        "item_name": "displayName",
    },
    "trace": {
        "name": "Cloud Trace",
        "category": "Operations",
        "endpoint": lambda p, r, z: f"https://cloudtrace.googleapis.com/v2/projects/{p}/traces",
        "description": "List traces",
        "result_key": "traces",
        "item_name": "traceId",
    },
    "errorreporting": {
        "name": "Error Reporting",
        "category": "Operations",
        "endpoint": lambda p, r, z: f"https://clouderrorreporting.googleapis.com/v1beta1/projects/{p}/groupStats",
        "description": "List error groups",
        "result_key": "errorGroupStats",
        "item_name": "group.groupId",
    },

    # ==================== MANAGEMENT ====================
    "resourcemanager": {
        "name": "Resource Manager",
        "category": "Management",
        "endpoint": lambda p, r, z: f"https://cloudresourcemanager.googleapis.com/v1/projects/{p}",
        "description": "Get project info",
        "result_key": None,
        "item_name": "projectId",
    },
    "billing": {
        "name": "Cloud Billing",
        "category": "Management",
        "endpoint": lambda p, r, z: f"https://cloudbilling.googleapis.com/v1/projects/{p}/billingInfo",
        "description": "Get billing info",
        "result_key": None,
        "item_name": "billingAccountName",
    },
    "serviceusage": {
        "name": "Service Usage",
        "category": "Management",
        "endpoint": lambda p, r, z: f"https://serviceusage.googleapis.com/v1/projects/{p}/services?filter=state:ENABLED",
        "description": "List enabled APIs",
        "result_key": "services",
        "item_name": "config.name",
    },

    # ==================== MIGRATION ====================
    "dms": {
        "name": "Database Migration Service",
        "category": "Migration",
        "endpoint": lambda p, r, z: f"https://datamigration.googleapis.com/v1/projects/{p}/locations/{r}/migrationJobs",
        "description": "List migration jobs",
        "result_key": "migrationJobs",
        "item_name": "name",
    },
    "transfer": {
        "name": "Storage Transfer Service",
        "category": "Migration",
        "endpoint": lambda p, r, z: f"https://storagetransfer.googleapis.com/v1/transferJobs?filter=%7B%22projectId%22%3A%22{p}%22%7D",
        "description": "List transfer jobs",
        "result_key": "transferJobs",
        "item_name": "name",
    },
}

# ============================================================
# Helper Functions
# ============================================================

def validate_config():
    """Validate required config."""
    required = {
        "AZURE_TENANT_ID": TENANT_ID,
        "AZURE_CLIENT_ID": CLIENT_ID,
        "AZURE_CLIENT_SECRET": CLIENT_SECRET,
        "GCP_PROJECT_ID": GCP_PROJECT_ID,
        "GCP_PROJECT_NUMBER": GCP_PROJECT_NUMBER,
        "GCP_SERVICE_ACCOUNT_EMAIL": SERVICE_ACCOUNT_EMAIL,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print("❌ Missing environment variables:")
        for var in missing:
            print(f"   - {var}")
        sys.exit(1)


def get_entra_token():
    """Get OIDC token from Entra ID - tries multiple scope formats."""
    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    
    # List of scopes to try (in order)
    scopes_to_try = [
        f"api://{CLIENT_ID}/.default",           # Standard custom API
        f"{CLIENT_ID}/.default",                  # Without api:// prefix
        "https://graph.microsoft.com/.default",   # Microsoft Graph (fallback)
    ]
    
    for scope in scopes_to_try:
        response = requests.post(token_url, data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": scope,
        })
        
        if response.status_code == 200:
            print(f"        (using scope: {scope})")
            return response.json()["access_token"]
        
        # If invalid_resource, try next scope
        if "invalid_resource" in response.text or "AADSTS500011" in response.text:
            continue
        else:
            # Other error, fail immediately
            break
    
    print(f"❌ Failed to get Entra token: {response.json()}")
    sys.exit(1)


def exchange_sts_token(entra_token):
    """Exchange Entra token via GCP STS."""
    audience = (
        f"//iam.googleapis.com/projects/{GCP_PROJECT_NUMBER}"
        f"/locations/global/workloadIdentityPools/{POOL_ID}"
        f"/providers/{PROVIDER_ID}"
    )
    response = requests.post("https://sts.googleapis.com/v1/token", json={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "audience": audience,
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "subject_token": entra_token,
    })
    if response.status_code != 200:
        print(f"❌ STS exchange failed: {response.json()}")
        sys.exit(1)
    return response.json()["access_token"]


def impersonate_sa(sts_token):
    """Impersonate Service Account."""
    url = (
        f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        f"{SERVICE_ACCOUNT_EMAIL}:generateAccessToken"
    )
    response = requests.post(url,
        headers={"Authorization": f"Bearer {sts_token}"},
        json={"scope": ["https://www.googleapis.com/auth/cloud-platform"], "lifetime": "3600s"}
    )
    if response.status_code != 200:
        print(f"❌ SA impersonation failed: {response.json()}")
        sys.exit(1)
    return response.json()["accessToken"]


def get_nested_value(obj, path):
    """Get nested value from dict using dot notation."""
    if not path:
        return None
    keys = path.split(".")
    for key in keys:
        if isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return obj


def test_service(service_key, access_token, region, zone, verbose=False):
    """Test a single GCP service."""
    service = SERVICES[service_key]
    endpoint = service["endpoint"](GCP_PROJECT_ID, region, zone)
    method = service.get("method", "GET")
    body = service.get("body", None)
    
    result = {
        "service": service_key,
        "name": service["name"],
        "category": service["category"],
        "description": service["description"],
        "status": "UNKNOWN",
        "count": 0,
        "items": [],
        "error": None,
    }
    
    try:
        headers = {"Authorization": f"Bearer {access_token}"}
        
        if method == "POST":
            response = requests.post(endpoint, headers=headers, json=body, timeout=30)
        else:
            response = requests.get(endpoint, headers=headers, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            result_key = service["result_key"]
            item_name = service["item_name"]
            
            if result_key is None:
                result["status"] = "PASS"
                result["count"] = 1
                if item_name:
                    val = get_nested_value(data, item_name)
                    if val:
                        result["items"] = [val]
            elif item_name is None:
                items = data.get(result_key, [])
                result["status"] = "PASS"
                result["count"] = len(items) if items else 0
                if items:
                    result["items"] = [str(i).split("/")[-1] if "/" in str(i) else str(i) for i in items[:5]]
            else:
                items = data.get(result_key, [])
                result["status"] = "PASS"
                result["count"] = len(items) if items else 0
                if items:
                    result["items"] = [get_nested_value(i, item_name) for i in items[:5]]
                
        elif response.status_code == 403:
            result["status"] = "NO_PERMISSION"
            error_msg = response.json().get("error", {}).get("message", "Access denied")
            result["error"] = error_msg[:100]
        elif response.status_code == 404:
            result["status"] = "NOT_FOUND"
            result["error"] = "API not enabled or resource not found"
        elif response.status_code == 400:
            error_data = response.json()
            if "INVALID_ARGUMENT" in str(error_data):
                result["status"] = "PASS"
                result["count"] = 0
            else:
                result["status"] = "ERROR"
                result["error"] = f"Bad request: {str(error_data)[:80]}"
        else:
            result["status"] = "ERROR"
            result["error"] = f"HTTP {response.status_code}"
            
    except requests.exceptions.Timeout:
        result["status"] = "TIMEOUT"
        result["error"] = "Request timed out"
    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)[:100]
    
    return result


def print_results(results, show_items=False):
    """Print test results in a nice table."""
    categories = {}
    for r in results:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(r)
    
    status_emoji = {
        "PASS": "✅",
        "NO_PERMISSION": "🔒",
        "NOT_FOUND": "❓",
        "TIMEOUT": "⏱️",
        "ERROR": "❌",
        "UNKNOWN": "❔",
    }
    
    summary = {"PASS": 0, "NO_PERMISSION": 0, "NOT_FOUND": 0, "ERROR": 0, "TIMEOUT": 0}
    
    print("\n" + "=" * 90)
    print("  TEST RESULTS")
    print("=" * 90)
    
    for category in sorted(categories.keys()):
        items = categories[category]
        print(f"\n  📁 {category}")
        print("  " + "-" * 86)
        
        for r in sorted(items, key=lambda x: x["name"]):
            emoji = status_emoji.get(r["status"], "❔")
            status = r["status"]
            summary[status] = summary.get(status, 0) + 1
            
            if status == "PASS":
                count_str = f"({r['count']} found)"
                print(f"    {emoji} {r['name']:<35} {count_str:<15} {r['description']}")
                if show_items and r["items"]:
                    for item in r["items"]:
                        if item:
                            print(f"       └─ {item}")
                    if r["count"] > 5:
                        print(f"       └─ ... and {r['count'] - 5} more")
            elif status == "NO_PERMISSION":
                print(f"    {emoji} {r['name']:<35} {'NO ACCESS':<15} SA tidak punya permission")
            elif status == "NOT_FOUND":
                print(f"    {emoji} {r['name']:<35} {'NOT ENABLED':<15} API belum enabled")
            else:
                print(f"    {emoji} {r['name']:<35} {status:<15} {r.get('error', '')[:35]}")
    
    print("\n" + "=" * 90)
    print("  SUMMARY")
    print("=" * 90)
    total = len(results)
    print(f"""
    Total services tested: {total}
    
    ✅ PASS (SA punya akses)     : {summary.get('PASS', 0)}
    🔒 NO_PERMISSION (403)       : {summary.get('NO_PERMISSION', 0)}
    ❓ NOT_FOUND/NOT_ENABLED     : {summary.get('NOT_FOUND', 0)}
    ❌ ERROR                     : {summary.get('ERROR', 0)}
    ⏱️  TIMEOUT                   : {summary.get('TIMEOUT', 0)}
    """)
    
    if summary.get('PASS', 0) > 0:
        print("    🎉 WIF berfungsi! SA bisa akses beberapa GCP services.")
    if summary.get('NO_PERMISSION', 0) > 0:
        print("    💡 Beberapa service NO_PERMISSION - normal kalau SA memang tidak punya role-nya.")


def list_services():
    """List all available services."""
    print("\n" + "=" * 90)
    print("  AVAILABLE GCP SERVICES FOR TESTING")
    print("=" * 90)
    
    categories = {}
    for key, svc in SERVICES.items():
        cat = svc["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append((key, svc))
    
    for category in sorted(categories.keys()):
        items = categories[category]
        print(f"\n  📁 {category} ({len(items)} services)")
        print("  " + "-" * 86)
        for key, svc in sorted(items, key=lambda x: x[1]["name"]):
            print(f"    {key:<30} {svc['name']:<35} {svc['description']}")
    
    print(f"\n  Total: {len(SERVICES)} services")
    print("\n  Usage examples:")
    print("    python test_gcp_wif_universal.py --all")
    print("    python test_gcp_wif_universal.py --services storage,bigquery,firebase")
    print("    python test_gcp_wif_universal.py --category firebase")


def main():
    parser = argparse.ArgumentParser(
        description="Universal GCP WIF Tester - Test access to 70+ GCP services",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Test all services:
    python test_gcp_wif_universal.py --all

  Test specific services:
    python test_gcp_wif_universal.py --services storage,bigquery,firebase

  Test by category:
    python test_gcp_wif_universal.py --category firebase
    python test_gcp_wif_universal.py --category "AI/ML"

  Test with verbose output:
    python test_gcp_wif_universal.py --all --verbose

  List available services:
    python test_gcp_wif_universal.py --list
        """
    )
    parser.add_argument("--all", action="store_true", help="Test all available services")
    parser.add_argument("--services", type=str, help="Comma-separated list of services to test")
    parser.add_argument("--category", type=str, help="Test all services in a category")
    parser.add_argument("--list", action="store_true", help="List all available services")
    parser.add_argument("--region", type=str, default=DEFAULT_REGION, help=f"GCP region (default: {DEFAULT_REGION})")
    parser.add_argument("--zone", type=str, default=DEFAULT_ZONE, help=f"GCP zone (default: {DEFAULT_ZONE})")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")
    parser.add_argument("--parallel", "-p", action="store_true", help="Run tests in parallel")
    
    args = parser.parse_args()
    
    if args.list:
        list_services()
        return
    
    if not args.all and not args.services and not args.category:
        parser.print_help()
        return
    
    # Determine which services to test
    if args.all:
        services_to_test = list(SERVICES.keys())
    elif args.category:
        cat = args.category.lower()
        services_to_test = [k for k, v in SERVICES.items() if v["category"].lower() == cat]
        if not services_to_test:
            print(f"❌ Category '{args.category}' not found.")
            print("   Available categories:", ", ".join(sorted(set(v["category"] for v in SERVICES.values()))))
            sys.exit(1)
    else:
        services_to_test = [s.strip() for s in args.services.split(",")]
        invalid = [s for s in services_to_test if s not in SERVICES]
        if invalid:
            print(f"❌ Unknown services: {', '.join(invalid)}")
            print("   Run with --list to see available services")
            sys.exit(1)
    
    print("=" * 90)
    print("  GCP Workload Identity Federation - Universal Tester")
    print("=" * 90)
    print(f"  Timestamp : {datetime.now().isoformat()}")
    print(f"  Project   : {GCP_PROJECT_ID}")
    print(f"  Region    : {args.region}")
    print(f"  Zone      : {args.zone}")
    print(f"  Services  : {len(services_to_test)} service(s)")
    
    validate_config()
    
    print("\n  [1/3] Getting Entra ID token...")
    entra_token = get_entra_token()
    print("        ✅ Done")
    
    print("  [2/3] Exchanging via GCP STS...")
    sts_token = exchange_sts_token(entra_token)
    print("        ✅ Done")
    
    print("  [3/3] Impersonating Service Account...")
    access_token = impersonate_sa(sts_token)
    print("        ✅ Done")
    
    print(f"\n  Testing {len(services_to_test)} services...")
    
    results = []
    if args.parallel:
        with ThreadPoolExecutor(max_workers=15) as executor:
            futures = {
                executor.submit(test_service, svc, access_token, args.region, args.zone): svc
                for svc in services_to_test
            }
            for future in as_completed(futures):
                results.append(future.result())
    else:
        for i, svc in enumerate(services_to_test, 1):
            print(f"    [{i}/{len(services_to_test)}] Testing {SERVICES[svc]['name'][:40]}...", end="\r")
            results.append(test_service(svc, access_token, args.region, args.zone))
        print(" " * 70, end="\r")
    
    results.sort(key=lambda x: (x["category"], x["name"]))
    print_results(results, show_items=args.verbose)
    
    print("\n  🔒 Semua akses via WIF - Tidak ada SA key file!")
    print("=" * 90)


if __name__ == "__main__":
    main()
