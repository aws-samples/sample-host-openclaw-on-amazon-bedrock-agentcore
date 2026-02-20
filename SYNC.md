# Sync Instructions

## Workflow
```
EC2 (dev) ⇄ Local Mac ⇄ GitLab (team)
```

- EC2: Dev environment
- Local Mac: Bridge for syncing
- GitLab: Team collaboration

## Remotes
- `ec2` - EC2 dev server
- `gitlab` - GitLab repo

## SSH to EC2
```bash
ssh -i "~/.ssh/claude-code-key.pem" ec2-user@ec2-13-238-67-88.ap-southeast-2.compute.amazonaws.com
```

## Daily Workflow

### EC2 → Local → GitLab (pull from EC2, push to GitLab)

```bash
# Pull latest from EC2
GIT_SSH_COMMAND="ssh -i ~/.ssh/claude-code-key.pem" git pull ec2 master

# Push to GitLab
git push gitlab master
```

### GitLab → Local → EC2 (pull from GitLab, push to EC2)

```bash
# Pull latest from GitLab
git pull gitlab master

# Push to EC2 (rsync - recommended)
rsync -avz --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude 'cdk.out' --exclude 'node_modules' \
  -e "ssh -i ~/.ssh/claude-code-key.pem" \
  ./ ec2-user@ec2-13-238-67-88.ap-southeast-2.compute.amazonaws.com:projects/openclaw-on-agentcore/
```

### Useful Commands

```bash
# Compare local vs EC2
git diff ec2/master

# Dry-run rsync (preview what will be copied)
rsync -avzn --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude 'cdk.out' --exclude 'node_modules' \
  -e "ssh -i ~/.ssh/claude-code-key.pem" \
  ./ ec2-user@ec2-13-238-67-88.ap-southeast-2.compute.amazonaws.com:projects/openclaw-on-agentcore/
```

## Initial Setup

### 1. Clone from EC2
```bash
scp -i ~/.ssh/claude-code-key.pem -r ec2-user@ec2-13-238-67-88.ap-southeast-2.compute.amazonaws.com:projects/openclaw-on-agentcore/* .
scp -i ~/.ssh/claude-code-key.pem -r ec2-user@ec2-13-238-67-88.ap-southeast-2.compute.amazonaws.com:projects/openclaw-on-agentcore/.git .
```

### 2. Add remotes
```bash
git remote add ec2 "ec2-user@ec2-13-238-67-88.ap-southeast-2.compute.amazonaws.com:projects/openclaw-on-agentcore"
git remote add gitlab git@gitlab.com:<your-path>/openclaw-on-agentcore.git
```
