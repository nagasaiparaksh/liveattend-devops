// LiveAttend - CI/CD pipeline
// GitHub -> tests -> Docker image -> Trivy scan -> Docker Hub -> Kubernetes (Minikube) -> [AWS: Terraform + Ansible] -> smoke tests
pipeline {
  agent any
  options {
    timestamps()
    ansiColor('xterm')
    disableConcurrentBuilds()
    timeout(time: 45, unit: 'MINUTES')
  }
  parameters {
    booleanParam(name: 'DEPLOY_K8S', defaultValue: true, description: 'Deploy to Kubernetes (Minikube on this laptop)')
    booleanParam(name: 'DEPLOY_AWS', defaultValue: false, description: 'Deploy to AWS (Terraform + Ansible) - needs a running lab session')
  }
  environment {
    IMAGE_REPO        = 'ynsp26/liveattend'
    LOCAL_IMAGE       = 'liveattend'
    K8S_NS            = 'liveattend'
    AWS_REGION        = 'us-east-1'
    TF_IN_AUTOMATION  = '1'
    TF_STATE          = '/var/jenkins_home/state/liveattend.tfstate'
    ANSIBLE_FORCE_COLOR = '1'
  }
  stages {
    stage('1. Checkout from GitHub') {
      steps {
        script {
          env.GIT_SHORT = sh(script: 'git rev-parse --short HEAD', returnStdout: true).trim()
          env.APP_VERSION = readFile('VERSION').trim()
          env.TAG = "${env.APP_VERSION}-${env.BUILD_NUMBER}"
        }
        sh 'git log -1 --pretty="Commit %h by %an: %s"; git remote get-url origin'
      }
    }
    stage('2. Unit & Integration Tests') {
      steps {
        dir('app') {
          sh '''
            python3 -m venv .venv
            .venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt
            .venv/bin/flake8 --max-line-length=160 --exclude=.venv,tests .
            .venv/bin/pytest -v --junitxml=test-results.xml tests
          '''
        }
      }
      post { always { junit 'app/test-results.xml' } }
    }
    stage('3. Build Docker Image') {
      steps {
        dir('app') {
          sh '''
            docker build -t "$LOCAL_IMAGE:$TAG" --build-arg APP_VERSION="$APP_VERSION" \
              --build-arg BUILD_NUMBER="$BUILD_NUMBER" --build-arg GIT_COMMIT="$GIT_COMMIT" .
            docker image ls "$LOCAL_IMAGE:$TAG"
          '''
        }
      }
    }
    stage('4. Security Scan (Trivy)') {
      steps {
        sh 'trivy image --quiet --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$LOCAL_IMAGE:$TAG"'
      }
    }
    stage('5. Push Image to Docker Hub') {
      steps {
        withCredentials([usernamePassword(credentialsId: 'dockerhub', usernameVariable: 'DH_USER', passwordVariable: 'DH_PASS')]) {
          sh '''
            echo "$DH_PASS" | docker login -u "$DH_USER" --password-stdin
            docker tag "$LOCAL_IMAGE:$TAG" "$IMAGE_REPO:$TAG"
            docker tag "$LOCAL_IMAGE:$TAG" "$IMAGE_REPO:latest"
            docker push -q "$IMAGE_REPO:$TAG"
            docker push -q "$IMAGE_REPO:latest"
            docker logout
            echo "Pushed https://hub.docker.com/r/$IMAGE_REPO (tag $TAG)"
          '''
        }
      }
    }
    stage('6. Deploy to Kubernetes') {
      when { expression { params.DEPLOY_K8S } }
      steps {
        sh '''
          set +x
          kubectl config current-context
          kubectl apply -f k8s/namespace.yaml
          # secrets are generated once, live only inside the cluster and never in Git
          if ! kubectl -n "$K8S_NS" get secret liveattend >/dev/null 2>&1; then
            kubectl -n "$K8S_NS" create secret generic liveattend \
              --from-literal=db-password="$(python3 -c 'import secrets;print(secrets.token_hex(16))')" \
              --from-literal=secret-key="$(python3 -c 'import secrets;print(secrets.token_hex(32))')" \
              --from-literal=admin-password="$(python3 -c 'import secrets;print(secrets.token_urlsafe(12))')"
          fi
          kubectl apply -f k8s/postgres.yaml
          sed "s|IMAGE_PLACEHOLDER|$IMAGE_REPO:$TAG|" k8s/app.yaml | kubectl apply -f -
          kubectl -n "$K8S_NS" rollout status deployment/postgres --timeout=180s
          kubectl -n "$K8S_NS" rollout status deployment/liveattend --timeout=240s
          kubectl -n "$K8S_NS" get pods,svc -o wide
        '''
      }
    }
    stage('7. Smoke Test (Kubernetes)') {
      when { expression { params.DEPLOY_K8S } }
      steps {
        sh '''
          set +x
          kubectl -n "$K8S_NS" get secret liveattend -o jsonpath='{.data.admin-password}' | base64 -d > "$WORKSPACE/.k8s-pw"
          kubectl -n "$K8S_NS" port-forward svc/liveattend 18080:80 >/tmp/pf.log 2>&1 &
          PF=$!
          trap 'kill $PF 2>/dev/null; rm -f "$WORKSPACE/.k8s-pw"' EXIT
          for i in $(seq 1 20); do curl -fsS http://localhost:18080/healthz 2>/dev/null | grep -q "\\"build\\":\\"$BUILD_NUMBER\\"" && break; sleep 2; done
          echo "Health:"; curl -fsS http://localhost:18080/healthz; echo
          python3 scripts/smoke_test.py http://localhost:18080 "$WORKSPACE/.k8s-pw"
        '''
      }
    }
    stage('8. Terraform - Provision AWS') {
      when { expression { params.DEPLOY_AWS } }
      steps {
        dir('infra/terraform') {
          sh '''
            aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table
            mkdir -p "$(dirname "$TF_STATE")"
            MYIP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
            terraform init -input=false -backend-config="path=$TF_STATE"
            terraform validate
            terraform plan -input=false -var "admin_cidr=$MYIP/32" -out=tfplan
            terraform apply -input=false -auto-approve tfplan
            terraform output
          '''
        }
      }
    }
    stage('9. Ansible - Configure & Deploy to AWS') {
      when { expression { params.DEPLOY_AWS } }
      steps {
        sh '''
          set +x
          cd infra/terraform
          umask 077
          terraform output -json | python3 -c '
import json,sys,os
o=json.load(sys.stdin)
json.dump({"image":os.environ["IMAGE_REPO"]+":"+os.environ["TAG"],"build_number":os.environ["BUILD_NUMBER"],
           "db_password":o["db_password"]["value"],"admin_password":o["admin_password"]["value"],
           "secret_key":o["secret_key"]["value"]}, open("../ansible/.generated/vars.json","w"))'
          terraform output -raw admin_password > "$(dirname "$TF_STATE")/admin-password"
          cd ../ansible
          ansible-playbook site.yml -e @.generated/vars.json
        '''
      }
      post { always { sh 'rm -f infra/ansible/.generated/vars.json' } }
    }
    stage('10. Smoke Test (AWS)') {
      when { expression { params.DEPLOY_AWS } }
      steps {
        dir('infra/terraform') {
          script { env.APP_URL = sh(script: 'terraform output -raw app_url', returnStdout: true).trim() }
          sh '''
            set +x
            for i in $(seq 1 20); do curl -fsS "$APP_URL/healthz" 2>/dev/null | grep -q "\\"build\\":\\"$BUILD_NUMBER\\"" && break; sleep 3; done
            echo "Health:"; curl -fsS "$APP_URL/healthz"; echo
            python3 ../../scripts/smoke_test.py "$APP_URL" "$(dirname "$TF_STATE")/admin-password"
          '''
        }
      }
    }
  }
  post {
    success {
      script {
        def where = []
        if (params.DEPLOY_K8S) { where << 'Kubernetes' }
        if (params.DEPLOY_AWS) { where << "AWS ${env.APP_URL}" }
        currentBuild.description = "v${env.TAG} -> ${where.join(' + ')}"
      }
      echo "LiveAttend v${env.TAG} deployed (commit ${env.GIT_SHORT}). Image: https://hub.docker.com/r/${env.IMAGE_REPO}"
    }
    failure { echo 'Deployment failed - see the red stage. Previous pods / containers keep running until the new ones are healthy.' }
  }
}
