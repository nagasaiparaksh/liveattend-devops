# LiveAttend - Live QR-code Attendance Tracker
**Mini Project · 21IPE333P Essentials in Cloud and DevOps · Yerramalli Naga Sai Paraksh (RA2311003011577)**

Faculty start a class session and project a QR code that **changes every 20 seconds**. Students scan it with their
phones, enter their register number and are marked present instantly. The signed, rotating token stops a photo of the
QR code from being shared with friends outside the room; one phone can mark only one register number.
Reports and CSV export are stored in PostgreSQL.

## Pipeline
```
git push / "Build Now" in Jenkins
  -> Jenkins pulls the repo from GitHub
  -> flake8 + pytest (17 tests)
  -> Docker image built
  -> Trivy security scan (blocks HIGH/CRITICAL vulnerabilities)
  -> image pushed to Docker Hub
  -> deployed to Kubernetes (Minikube): app x2 replicas + PostgreSQL, secrets generated in-cluster
  -> optional: Terraform builds AWS (VPC, EC2, Elastic IP, CloudWatch alarms), Ansible deploys with Docker Compose
  -> end-to-end smoke test: faculty logs in, a "phone" scans the QR, attendance appears live
```

| Concept | Where |
|---|---|
| Git / GitHub | this repo, Jenkins polls it |
| Jenkins (CI/CD) | `Jenkinsfile`, `Jenkinsfile.destroy`, `jenkins/` (configured as code: JCasC + Job DSL) |
| Docker | `app/Dockerfile`, `docker-compose.yml`, images on Docker Hub |
| Security scan | Trivy stage |
| Kubernetes | `k8s/` (Deployment, Service, PVC, Secret) on Minikube |
| Terraform | `infra/terraform/` (VPC, subnet, IGW, security group, EC2, Elastic IP, CloudWatch alarms) |
| Ansible | `infra/ansible/` (roles `docker`, `liveattend`) |
| AWS | EC2, VPC, Elastic IP, CloudWatch |

## Run locally
```bash
docker compose up --build --watch      # http://localhost:8200   login: admin / admin-change-me
```
Tests: `cd app && pip install -r requirements.txt -r requirements-dev.txt && pytest -v`
