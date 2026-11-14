#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Fork 自动同步脚本（自动发现版 · 安全加固）

工作方式：
1. 拉取 mojinhai 名下所有仓库
2. 【安全第1层】跳过所有私有仓库（private=True），绝不触碰
3. 【安全第2层】跳过非 fork 仓库（fork=False），自己建的不动
4. 【安全第3层】跳过 skip.txt 黑名单里的仓库
5. 对剩余 fork 逐个执行 merge-upstream 同步

你新 fork 任何公开项目，第二天会自动发现并同步。
你自己新建的、私有的项目永远安全。
"""
import os
import sys
import json
import time
import re
import urllib.request
import urllib.error

OWNER = 'mojinhai'
TOKEN = os.environ.get('SYNC_TOKEN', '')
API = 'https://api.github.com'
HDR = {
    'Authorization': 'token ' + TOKEN,
    'User-Agent': 'fork-sync-bot',
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
}
HERE = os.path.dirname(os.path.abspath(__file__))

# 自动产生的提交特征：命中则视为「非用户手写」，可安全继续合并
AUTO_PATTERNS = [
    re.compile(r'^chore:\s*sync from upstream', re.I),
    re.compile(r'^Merge (branch|remote-tracking branch|pull request)', re.I),
    re.compile(r"^Merge branch '.*' into .*", re.I),
    re.compile(r'^Merge .+ into .+', re.I),
]

# 默认跳过名单（与 skip.txt 叠加）
DEFAULT_SKIP = {
    'Kwrt',
    'immortalwrt-mt798x-6.6',
    'OpenWRT-Mediatek',
    'Xray_bash_onekey',
}


def api(method, path, body=None):
    """调用 GitHub API，返回 (状态码, 解析后的 JSON 或文本)"""
    data = json.dumps(body).encode() if body is not None else None
    h = dict(HDR)
    if data:
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return 0, str(e)


def load_skip():
    """读取 skip.txt 黑名单，与默认名单合并"""
    skip = set(DEFAULT_SKIP)
    path = os.path.join(HERE, 'skip.txt')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    skip.add(line)
    return skip


def discover_forks():
    """
    自动发现所有可同步的 fork。
    返回 (候选列表, 私有仓库名列表, 非fork仓库名列表)
    候选列表元素：(fork名, 上游owner/repo, 默认分支)
    """
    found, privates, non_forks = [], [], []
    page = 1
    while True:
        st, repos = api('GET', '/user/repos?per_page=100&page=%d'
                        '&affiliation=owner&sort=updated' % page)
        if st != 200 or not isinstance(repos, list):
            print('  ⚠️ 第 %d 页拉取失败：%s' % (page, st))
            break
        if not repos:
            break
        for r in repos:
            name = r.get('name')

            # ── 安全第1层：私有仓库直接跳过，绝不查询、绝不修改 ──
            if r.get('private'):
                privates.append(name)
                continue

            # ── 安全第2层：非 fork 仓库跳过（自己建的） ──
            if not r.get('fork'):
                non_forks.append(name)
                continue

            # 是公开 fork，查上游 parent
            st2, d = api('GET', '/repos/%s/%s' % (OWNER, name))
            if st2 != 200 or not isinstance(d, dict):
                continue
            # 再次确认：即便拿到详情，私有也绝不处理
            if d.get('private'):
                if name not in privates:
                    privates.append(name)
                continue
            if not d.get('fork'):
                if name not in non_forks:
                    non_forks.append(name)
                continue
            parent = (d.get('parent') or {}).get('full_name')
            if not parent:
                continue
            found.append((name, parent, d.get('default_branch')))
            time.sleep(0.3)
        if len(repos) < 100:
            break
        page += 1
    return found, privates, non_forks


def is_auto_commit(msg):
    """判断提交信息是否属于自动产生（非用户手写）"""
    first = msg.split('\n')[0].strip()
    return any(p.search(first) for p in AUTO_PATTERNS)


def compare(upstream, up_branch, fork_branch):
    """返回 compare 结果 dict，失败返回 None"""
    st, d = api('GET', '/repos/%s/compare/%s...%s:%s'
                % (upstream, up_branch, OWNER, fork_branch))
    if st == 200 and isinstance(d, dict):
        return d
    return None


def has_manual_commits(fork, fork_branch, upstream):
    """检查 fork 上是否存在用户手写的提交（不在上游的）"""
    st, recent = api('GET', '/repos/%s/commits?sha=%s&per_page=30'
                     % (fork, fork_branch))
    if st != 200 or not isinstance(recent, list):
        return None
    for cm in recent:
        sha = cm.get('sha')
        st2, _ = api('GET', '/repos/%s/commits/%s' % (upstream, sha))
        if st2 == 200:
            continue
        msg = cm.get('commit', {}).get('message', '')
        if not is_auto_commit(msg):
            return msg.split('\n')[0][:50]
    return None


def sync_one(fork, upstream, fork_branch):
    """同步单个 fork，返回 (状态, 说明)"""
    st, u = api('GET', '/repos/' + upstream)
    if st != 200 or not isinstance(u, dict):
        return 'skip', '上游仓库不存在或无法访问'
    up_branch = u.get('default_branch')
    if not fork_branch:
        return 'skip', 'fork 无默认分支'

    c = compare(upstream, up_branch, fork_branch)
    if c is None:
        return 'skip', '无法比较（可能无共同祖先）'
    behind = c.get('behind_by', 0)
    ahead = c.get('ahead_by', 0)
    status = c.get('status')

    if status == 'identical' or (behind == 0 and ahead == 0):
        return 'ok', '已是最新'
    if behind == 0:
        return 'ok', '已是最新（本地领先 %d 个提交）' % ahead

    if status == 'diverged' and ahead > 0:
        manual = has_manual_commits(fork, fork_branch, upstream)
        if manual:
            return 'watch', '有手写提交（%s），落后 %d 需人工处理' % (manual, behind)

    st, d = api('POST', '/repos/%s/%s/merge-upstream' % (OWNER, fork),
                {'branch': fork_branch})
    if st == 200 and isinstance(d, dict):
        mt = d.get('merge_type', '?')
        if mt == 'none':
            return 'ok', '已是最新'
        return 'ok', '同步成功（%s，落后 %d → 0）' % (mt, behind)
    if st == 409:
        return 'watch', '存在冲突（落后 %d / 领先 %d）' % (behind, ahead)
    if st == 422:
        msg = d.get('message', '') if isinstance(d, dict) else str(d)
        return 'watch', '被拒绝：%s' % str(msg)[:100]
    msg = d.get('message', '') if isinstance(d, dict) else str(d)
    return 'fail', 'HTTP %s：%s' % (st, str(msg)[:100])


def main():
    if not TOKEN:
        print('❌ 缺少 SYNC_TOKEN 环境变量')
        sys.exit(1)

    print('=' * 64)
    print('GitHub Fork 自动同步（自动发现 · 已启用私有仓库保护）')
    print('=' * 64)

    print('\n🔍 扫描仓库中...')
    all_forks, privates, non_forks = discover_forks()
    print('   可同步的公开 fork：%d 个' % len(all_forks))
    print('   🔒 私有仓库（已保护，绝不触碰）：%d 个' % len(privates))
    if privates:
        for p in privates:
            print('      - %s' % p)
    print('   🏠 自己建的公开仓库（不动）：%d 个' % len(non_forks))
    if non_forks:
        for p in non_forks:
            print('      - %s' % p)

    skip = load_skip()
    do_list, watch_list = [], []
    for item in all_forks:
        if item[0] in skip:
            watch_list.append(item)
        else:
            do_list.append(item)

    print('\n   实际同步：%d 个 | 黑名单跳过：%d 个'
          % (len(do_list), len(watch_list)))

    results = {'ok': [], 'skip': [], 'watch': [], 'fail': []}

    for fork, upstream, branch in do_list:
        print('\n▶ %s  ←  %s' % (fork, upstream))
        try:
            status, msg = sync_one(fork, upstream, branch)
        except Exception as e:
            status, msg = 'fail', '异常：%s' % str(e)[:100]
        icon = {'ok': '✅', 'skip': '⏭️', 'watch': '👀', 'fail': '❌'}[status]
        print('  %s %s' % (icon, msg))
        results[status].append((fork, msg))
        time.sleep(1)

    if watch_list:
        print('\n' + '-' * 64)
        print('👀 黑名单（不做任何改动）')
        print('-' * 64)
        for fork, upstream, branch in watch_list:
            st, u = api('GET', '/repos/' + upstream)
            if st != 200 or not isinstance(u, dict):
                print('  ⛔ %-24s 无上游（已删库/封号），保持原样' % fork)
                continue
            c = compare(upstream, u.get('default_branch'), branch)
            if c is None:
                print('  ⚠️ %-24s 无法比较（无共同祖先）' % fork)
            else:
                print('  👀 %-24s 落后 %-6s 领先 %-6s'
                      % (fork, c.get('behind_by'), c.get('ahead_by')))
            time.sleep(0.6)

    print('\n' + '=' * 64)
    print('📊 汇总：成功 %d | 跳过 %d | 需人工 %d | 失败 %d'
          % (len(results['ok']), len(results['skip']),
             len(results['watch']), len(results['fail'])))
    print('🔒 私有仓库保护：%d 个已排除，未被触碰' % len(privates))
    print('=' * 64)

    if results['fail']:
        sys.exit(1)


if __name__ == '__main__':
    main()