# Helm chart `nlm-k2`

## Deploy GKE dengan script

Default script: release `nilam-ocr-kk`, prefix image `nilam-ocr-kk`, namespace `nlm-k2`.
Deployment, Service, dan image komponen bernama `nilam-ocr-kk-<service>`.
Script mengisi repository dan tag Helm sesuai image yang dibangun, mendukung install
pertama (`all` wajib) maupun upgrade sebagian service. Registry dan prefix image dapat
diubah lewat `REGISTRY` dan `IMAGE_PREFIX`.

Di Windows gunakan Git Bash dengan Docker Desktop Linux containers, Helm, kubectl,
Google Cloud SDK, dan Python dengan `python-dotenv` tersedia di PATH:

```bash
cd /d/ocr-kk/nlm-k2
gcloud auth login
gcloud container clusters get-credentials gc-ddb-dev-gke-cluster-01 \
  --project ddb-kubecluster-dev-01 --location asia-southeast2
PYTHON=.venv/Scripts/python.exe bash deploy/helm/deploy.sh --dry-run all
PYTHON=.venv/Scripts/python.exe bash deploy/helm/deploy.sh all
```

Deploy nyata harus dari `main` bersih yang sudah di-push dan sama dengan `origin/main`.
Install ini membuat release baru jika `nilam-ocr-kk` belum ada; release lama `nlm-k2`
tidak dihapus. Untuk memeriksa hasil:

```bash
kubectl -n nlm-k2 get deploy,pods,svc
kubectl -n nlm-k2 port-forward svc/nilam-ocr-kk-orchestrator 8040:8040
```

Migrasi setelah install menggunakan
`SECRET=nilam-ocr-kk-extraction-env DB_HOST=<alamat> deploy/helm/migrate-db.sh`.
Pastikan skema database sudah sesuai sebelum service menerima traffic.

## Konfigurasi dari `.env` produksi

`deploy.sh` membaca `services/<service>/.env` untuk kelima service, bukan `.env.local`.
Python dengan paket `python-dotenv` diperlukan (`PYTHON` dapat menunjuk interpreter venv).
Konfigurasi dimasukkan ke Secret per service; dalam mode ini ConfigMap dan Secret lama
tidak menjadi sumber environment container. Perubahan `.env` me-restart service terkait,
termasuk service yang tidak dipilih untuk build. Nilai disimpan dalam Secret Kubernetes
dan riwayat release Helm; batasi akses keduanya.

URL upstream yang kosong atau localhost diterjemahkan menjadi DNS Service release.
URL eksternal yang sudah diisi dipertahankan. Nama release harus sesuai fullname chart
(jangan gunakan `nameOverride`/`fullnameOverride` dalam mode ini).

Untuk install pertama gunakan script agar repository dan tag seluruh image ikut diisi:

```bash
PYTHON=.venv/Scripts/python.exe bash deploy/helm/deploy.sh all
```

File sementara berisi rahasia; jangan commit atau bagikan. Dry-run deploy script
memvalidasi manifest tanpa mencetak Secret. Perintah Helm langsung tanpa generated values
tetap memakai konfigurasi values/Secret lama yang dijelaskan di bawah.

Setelah deploy memakai `.env`, migrasi dari cluster mengambil Secret extraction:
`SECRET=nilam-ocr-kk-extraction-env DB_HOST=<alamat> deploy/helm/migrate-db.sh`.

Satu Deployment, Service, ConfigMap, PodDisruptionBudget, dan NetworkPolicy per service:
`orchestrator` (8040), `guardrails` (8041), `extraction` (8042), `structuring` (8043), `scoring`
(8044). Nama objeknya `<release>-<service>`, jadi di cluster dev: `nlm-k2-orchestrator`, dst.
Tiap service bisa di-scale dan di-restart sendiri; guardrails (torch, CPU-bound) punya HPA opsional, dan
kalau ia kehabisan memori, tahap lain tidak ikut jatuh.

Service `nlm-k2` (tanpa akhiran) adalah pintu masuk yang dipublikasikan ke Orkestrasi pusat
([integration.md](../../integration.md)): ia membuka port semua service ber-`entrypoint`, yaitu hanya
orchestrator (8040). Selector-nya mencakup semua pod release, tetapi port itu memakai `targetPort`
bernama (`orchestrator`), dan Kubernetes hanya memasukkan pod yang punya nama port itu ke endpoint
port tersebut. Karena nama service dipakai sebagai nama port, nama service maksimal 15 karakter, huruf
kecil/angka, tanpa `-`. URL antar service di dalam release memakai Service per komponen
(`http://nlm-k2-structuring:8043`).

Ini satu-satunya jalur deploy; manifest Kustomize yang dulu ada di `deploy/k8s` sudah dihapus
dan polanya (Deployment per service, NetworkPolicy, PDB, HPA, ExternalSecret) dibawa ke sini.

## Yang sudah diverifikasi di `gc-ddb-dev-gke-cluster-01`

Dari pod di namespace `nlm-k2`:

| Tujuan | Alamat | Hasil |
|---|---|---|
| Image di Artifact Registry | `common-cicd-dev-01/gc-bribrain-dev-gar-temp-01` | bisa ditarik tanpa `imagePullSecrets` |
| PostgreSQL (dalam cluster) | `postgres.ocr-dev.svc.cluster.local:5432` | terjangkau |
| PostgreSQL (LoadBalancer yang sama) | `34.50.114.49:5432` | terjangkau |
| Orkestrasi | `ocr-orchestration.ocr-dev.svc.cluster.local:80` | terjangkau |
| PaddleOCR | `10.213.128.67:8070` | terjangkau |

Cluster dev memakai `LEGACY_DATAPATH` tanpa network policy enforcement: NetworkPolicy chart ini
terpasang di sana tetapi belum ditegakkan. Di cluster dengan Dataplane V2 (atau Calico) ia
langsung berlaku, jadi `networkPolicy.clientNamespaces` harus terisi sebelum Orkestrasi memanggil.

## Objek yang dirender

| Objek | Per service | Value |
|---|---|---|
| Deployment `<release>-<svc>` | ya | `services.<svc>.replicaCount`, `resources`, `terminationGracePeriodSeconds` |
| Service `<release>-<svc>` | ya | `service.type`, `service.annotations` |
| Service `<release>` (pintu masuk) | tidak; port dari service ber-`entrypoint` | `service.entrypoint.enabled` |
| ConfigMap `<release>-<svc>` | ya | `environment`, `orchestration`, `commonEnv`, `services.<svc>.env`, `upstreams` |
| PodDisruptionBudget | ya | `podDisruptionBudget.{enabled,maxUnavailable}` |
| HorizontalPodAutoscaler | hanya yang `autoscaling.enabled` | `services.<svc>.autoscaling.{minReplicas,maxReplicas,targetCPUUtilizationPercentage}` |
| NetworkPolicy | default-deny + satu per service | `networkPolicy.{enabled,clientNamespaces}` |
| ServiceAccount | satu untuk semua | `serviceAccount.{create,name,annotations}` |
| ExternalSecret | opsional, mati | `externalSecret.*` |

Aturan NetworkPolicy dihitung dari `services.<svc>.upstreams`: guardrails dan extraction hanya
menerima dari orchestrator, `structuring` dari extraction dan orchestrator, `scoring` dari structuring
dan orchestrator (orchestrator membaca status tiap tahap). Service ber-`entrypoint` (hanya
orchestrator) menerima dari semua pod release dan dari namespace di `networkPolicy.clientNamespaces`.
Egress belum dibatasi (utang teknis di README utama).

## Konfigurasi dinamis

Semua environment variable non-rahasia berasal dari `values`. Chart merender satu ConfigMap per
service, dan hash isinya dipasang sebagai annotation pod, jadi `helm upgrade` dengan nilai baru
hanya me-restart pod service yang konfigurasinya berubah.

| Value | Isi |
|---|---|
| `environment` | `ENVIRONMENT` untuk semua service (`dev`, `staging`, `production`) |
| `image.registry`, `image.tag` | registry dan tag bersama; `services.<nama>.image.tag` menimpa per service |
| `orchestration.url` | callback ke Orkestrasi; kosong = hasil lewat `ORCHESTRATION_OUTCOME_TABLE` (salah satu wajib) |
| `commonEnv` | env var untuk kelima service (yang tidak dikenal sebuah service diabaikan) |
| `services.<nama>.env` | env var per service; menimpa `commonEnv` dan nilai bawaan chart |
| `services.<nama>.upstreams` | service lain yang dipanggil; chart mengisi `<NAMA>_SERVICE_URL` dan NetworkPolicy |
| `services.<nama>.entrypoint` | dipanggil dari luar release: ikut Service pintu masuk dan menerima `clientNamespaces` |
| `services.<nama>.pipeline` | tahap async: menerima `DATABASE_URL` dan konfigurasi Orkestrasi |
| `services.<nama>.replicaCount` | jumlah pod; diabaikan kalau `autoscaling.enabled` |
| `services.<nama>.resources` | request dan limit per container |
| `services.<nama>.enabled` | matikan satu service beserta semua objeknya |
| `existingSecret`, `secretKeys` | nama Secret dan nama key di dalamnya |

Tulis angka sebagai string (`"0.8"`, `"5242880"`), karena Helm mengubah angka besar menjadi notasi ilmiah.

URL antar-service memakai nama Service per komponen, bukan `localhost`. Di luar
`ENVIRONMENT=local`, service menolak `*_SERVICE_URL` yang menunjuk ke localhost (orchestrator: keempat
URL-nya; extraction dan structuring: tahap berikutnya).

## Secret

Dengan `externalSecret.enabled=false` (default) chart tidak membuat Secret. Buat sendiri sebelum
atau sesudah install; pod menunggu sampai Secret ada.

```powershell
kubectl -n nlm-k2 create secret generic nlm-k2-secrets `
  --from-literal=API_KEY='<api-key>' `
  --from-literal=DATABASE_URL='postgresql+asyncpg://<user>:<password>@postgres.ocr-dev.svc.cluster.local:5432/bribrain_ocr_kk' `
  --from-literal=ORCHESTRATION_API_KEY='<opsional>'
```

`DATABASE_URL` harus berformat SQLAlchemy (`postgresql+asyncpg://`), bukan JDBC. Karakter khusus
di password perlu di-URL-encode (`@` menjadi `%40`). `ORCHESTRATION_API_KEY` boleh dihilangkan.
`orchestrator` dan `guardrails` hanya menerima `API_KEY`; `DATABASE_URL` tidak pernah masuk ke pod-nya. Key opsional `API_KEYS`
(dipisah koma) diterima juga oleh semua service selama rotasi: tambahkan key baru di sana, pindahkan
pemanggil, lalu jadikan `API_KEY` dan hapus dari `API_KEYS`; tiap langkah cukup `rollout restart`.

Setelah mengubah Secret, restart pod yang memakainya:
`kubectl -n nlm-k2 rollout restart deploy -l app.kubernetes.io/instance=nlm-k2`.

Cluster dengan External Secrets Operator dan `ClusterSecretStore` bisa memakai
`externalSecret.enabled=true`; Secret dengan nama `existingSecret` lalu diisi dari Secret Manager
(nama key di `externalSecret.remoteKeys`). Cluster dev tidak punya CRD ESO.

## Skema database

Service tidak membuat tabel sendiri; tabel dipasang lewat migrasi Alembic ([db/](../../db)).
Dari laptop:

```bash
DB_HOST=<alamat postgres> ./migrate-db.sh          # upgrade head
DB_HOST=<alamat postgres> ./migrate-db.sh current  # revisi yang terpasang sekarang
```

Script mengambil `DATABASE_URL` dari Secret release, mengganti host-nya dengan `DB_HOST`,
lalu menjalankan Alembic di dalam image `db/Dockerfile`. Database yang tabelnya sudah
dipasang manual sebelum ada migrasi aman dijalankan: revisi baseline memakai
`CREATE TABLE IF NOT EXISTS`.

## Deploy perubahan kode

Install pertama tetap memakai perintah di bagian berikutnya. Setelah release ada, perubahan kode
dideploy dengan [deploy.sh](deploy.sh) dari Git Bash, WSL, Linux atau macOS:

```bash
deploy/helm/deploy.sh --dry-run --skip-build --tag <tag> scoring
deploy/helm/deploy.sh scoring
deploy/helm/deploy.sh extraction structuring scoring
deploy/helm/deploy.sh all
```

**Deploy hanya dari `main`.** Script menolak `helm upgrade` kalau checkout bukan branch `main`,
berbeda dengan `origin/main` (belum di-pull atau ada commit yang belum di-push), atau ada perubahan
yang belum di-commit di `libs/`, `services/`, `db/`, atau `deploy/helm/`. Jadi yang jalan di cluster
selalu commit yang sudah ada di `main`; hotfix pun di-commit dan di-push ke `main` dulu. `--tag` untuk
deploy harus SHA commit di `origin/main` (commit selain HEAD hanya bersama `--skip-build`, misalnya
kembali ke versi lama). Dari branch lain hanya `--build-only` dan `--dry-run` yang jalan.

Script membangun image service yang disebut, mendorongnya ke Artifact Registry dengan tag
`git rev-parse --short HEAD` (`--build-only --allow-dirty` dari working tree yang belum di-commit
menambah `-dirty-<waktu>`; image seperti itu tidak bisa di-deploy), lalu menjalankan `helm upgrade --reset-then-reuse-values
-f values-ddb-dev.yaml --set services.<nama>.image.tag=<tag>`. Service lain tetap di tag lamanya
dan pod-nya tidak disentuh: hanya Deployment service yang disebut yang rolling update. Upgrade
menunggu pod siap dan otomatis rollback kalau gagal.

Perubahan `libs/ocr_common` masuk ke semua image; deploy `all`. Perubahan tabel dijalankan dulu
dengan [migrate-db.sh](migrate-db.sh) sebelum deploy image yang membutuhkannya.

**Riwayat release.** Setiap `helm upgrade` atau `helm rollback` menyimpan satu revisi sebagai Secret
`sh.helm.release.v1.nlm-k2.v<N>` di namespace; itulah yang dipakai `helm rollback`. `deploy.sh`
menyimpan 5 revisi terakhir (`--history-max`, ubah lewat env `HISTORY_MAX`); revisi yang lebih tua
dibuang Helm sendiri pada upgrade berikutnya, jadi Secret itu tidak perlu dihapus manual.

**Upgrade ke chart 0.3.0 (orchestrator sebagai pintu masuk): wajib `deploy.sh all`.** Values baru
mengubah ConfigMap guardrails (tanpa `*_SERVICE_URL` dan `PIPELINE_WAIT_SECONDS`), jadi annotation
`checksum/config` me-roll pod guardrails walau guardrails tidak disebut. Image guardrails lama menolak
start dengan ConfigMap itu (`ENVIRONMENT=production` dan `EXTRACTION_SERVICE_URL` default localhost),
lalu `--atomic` me-rollback seluruh release. Tag global `values-ddb-dev.yaml` juga tidak punya image
orchestrator. Sejak upgrade, entry Service `nlm-k2` hanya membuka 8040: Orkestrasi pusat harus
pindah dari `:8041/v1/extract-ocr` ke `:8040/v1/extract-ocr` pada saat yang sama. `helm rollback` ke
revisi sebelumnya membuang port 8040 lagi, jadi Orkestrasi pusat harus kembali ke `:8041` kalau
rollback.

## Install dan upgrade

```powershell
helm upgrade --install nlm-k2 deploy/helm/nlm-k2 `
  -n nlm-k2 --create-namespace -f deploy/helm/nlm-k2/values-ddb-dev.yaml
```

Mengganti tag image atau satu env var tanpa menyentuh file:

```powershell
helm upgrade nlm-k2 deploy/helm/nlm-k2 -n nlm-k2 --reuse-values `
  --set image.tag=<sha> --set services.scoring.env.SCORING_APPROVE_THRESHOLD="0.85"
```

Rollback: `helm -n nlm-k2 rollback nlm-k2`.

**Upgrade dari chart 0.1.x (satu pod, empat container).** Helm menghapus Deployment
`nlm-k2` yang lama dan membuat empat Deployment baru dalam satu `helm upgrade`. Pod lama
hilang saat pod baru masih starting (guardrails butuh sekitar satu menit memuat model), jadi ada
jeda singkat tanpa layanan; lakukan di luar jam uji coba. Values lama tetap dipakai
(`--reset-then-reuse-values`), termasuk tag image per service.

## Akses cluster

`gcloud container clusters get-credentials gc-ddb-dev-gke-cluster-01 --project ddb-kubecluster-dev-01 --location asia-southeast2`
butuh `gke-gcloud-auth-plugin`, yang dipasang dengan `gcloud components install gke-gcloud-auth-plugin`
dari terminal Administrator.

## Cek cepat

```powershell
kubectl -n nlm-k2 get deploy,pods,svc,pdb,networkpolicy
kubectl -n nlm-k2 port-forward svc/nlm-k2-orchestrator 8040:8040
curl http://localhost:8040/ready
```

`port-forward` harus ke Service per komponen (atau `deploy/nlm-k2-<service>`): pada Service
pintu masuk `nlm-k2`, kubectl memilih satu pod sembarang dari selector-nya.
