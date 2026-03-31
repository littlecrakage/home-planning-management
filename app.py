from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from datetime import date, timedelta

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///home_planning.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'home-planning-2026'
db = SQLAlchemy(app)


# ── Models ─────────────────────────────────────────────────────────────────────

class Home(db.Model):
    id   = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    icon = db.Column(db.String(10), default='🏠')


class Member(db.Model):
    id      = db.Column(db.Integer, primary_key=True)
    name    = db.Column(db.String(100), nullable=False)
    color   = db.Column(db.String(7), default='#4A90E2')
    active  = db.Column(db.Boolean, default=True)
    home_id = db.Column(db.Integer, db.ForeignKey('home.id'), nullable=False, default=1)


class Category(db.Model):
    id      = db.Column(db.Integer, primary_key=True)
    name    = db.Column(db.String(100), nullable=False)
    home_id = db.Column(db.Integer, db.ForeignKey('home.id'), nullable=False, default=1)


class Task(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(200), nullable=False)
    importance   = db.Column(db.Integer, default=3)
    effort       = db.Column(db.Integer, default=3)
    recurrence   = db.Column(db.String(20), default='weekly')
    category     = db.Column(db.String(100), default='')
    requires_two = db.Column(db.Boolean, default=False)
    active       = db.Column(db.Boolean, default=True)
    home_id      = db.Column(db.Integer, db.ForeignKey('home.id'), nullable=False, default=1)


class Assignment(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    task_id       = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    member_id     = db.Column(db.Integer, db.ForeignKey('member.id'), nullable=False)
    assigned_date = db.Column(db.Date, nullable=False)
    week_start    = db.Column(db.Date, nullable=False)
    completed     = db.Column(db.Boolean, default=False)

    task   = db.relationship('Task',   backref=db.backref('assignments', lazy=True))
    member = db.relationship('Member', backref=db.backref('assignments', lazy=True))


# ── Context processor ─────────────────────────────────────────────────────────

@app.context_processor
def inject_homes():
    return {'all_homes': Home.query.order_by(Home.id).all()}


# ── Helpers ────────────────────────────────────────────────────────────────────

def remaining_week_days(from_date=None):
    """Today through Sunday (inclusive)."""
    d = from_date or date.today()
    days = []
    while True:
        days.append(d)
        if d.weekday() == 6:
            break
        d += timedelta(days=1)
    return days


def week_start_of(d=None):
    d = d or date.today()
    return d - timedelta(days=d.weekday())


def priority_score(task):
    return (task.importance ** 2) / max(task.effort, 1)


# ── Planning algorithm ─────────────────────────────────────────────────────────

def generate_plan(hid, for_next_week=False):
    today = date.today()
    if for_next_week:
        wk_start  = week_start_of(today) + timedelta(weeks=1)
        remaining = [wk_start + timedelta(days=i) for i in range(7)]
    else:
        wk_start  = week_start_of(today)
        remaining = remaining_week_days(today)
    members   = Member.query.filter_by(active=True, home_id=hid).order_by(Member.id).all()

    if not members:
        return False, "No active household members. Add members first."

    # Sub-query of all task IDs belonging to this home
    home_task_ids_sq = db.session.query(Task.id).filter(Task.home_id == hid).scalar_subquery()

    # Wipe existing non-completed assignments for remaining days (this home only)
    for day in remaining:
        (Assignment.query
         .filter(
             Assignment.assigned_date == day,
             Assignment.completed == False,
             Assignment.task_id.in_(home_task_ids_sq)
         )
         .delete(synchronize_session=False))

    # Also wipe period-only (biweekly/monthly) assignments for this week
    period_task_ids = [r[0] for r in db.session.query(Task.id).filter(
        Task.home_id == hid,
        Task.recurrence.in_(['biweekly', 'monthly'])
    ).all()]
    if period_task_ids:
        (Assignment.query
         .filter(
             Assignment.week_start == wk_start,
             Assignment.completed == False,
             Assignment.task_id.in_(period_task_ids)
         )
         .delete(synchronize_session=False))
    db.session.flush()

    # ── Daily tasks: rotating assignment ─────────────────────────────────────
    daily_tasks = sorted(
        Task.query.filter_by(recurrence='daily', active=True, home_id=hid).all(),
        key=lambda t: t.effort, reverse=True
    )

    n_days    = len(remaining)
    n_members = len(members)
    week_effort    = {m.id: 0 for m in members}
    per_day_effort = {d: {m.id: 0 for m in members} for d in remaining}

    for task in daily_tasks:
        if task.requires_two:
            for day in remaining:
                for m in members:
                    week_effort[m.id]         += task.effort
                    per_day_effort[day][m.id] += task.effort
                    db.session.add(Assignment(
                        task_id=task.id, member_id=m.id,
                        assigned_date=day, week_start=wk_start))
            continue

        best_offset, best_score = 0, float('inf')
        for offset in range(n_members):
            day_counts = [0] * n_members
            for d in range(n_days):
                day_counts[(d + offset) % n_members] += 1

            projected_week = {
                m.id: week_effort[m.id] + day_counts[i] * task.effort
                for i, m in enumerate(members)
            }
            weekly_imbalance = max(projected_week.values()) - min(projected_week.values())

            daily_imbalance = 0
            for d_idx, day in enumerate(remaining):
                projected_day = dict(per_day_effort[day])
                projected_day[members[(d_idx + offset) % n_members].id] += task.effort
                daily_imbalance += max(projected_day.values()) - min(projected_day.values())

            score = weekly_imbalance * n_days + daily_imbalance
            if score < best_score:
                best_score  = score
                best_offset = offset

        day_counts = [0] * n_members
        for d_idx, day in enumerate(remaining):
            idx = (d_idx + best_offset) % n_members
            m   = members[idx]
            day_counts[idx] += 1
            per_day_effort[day][m.id] += task.effort
            db.session.add(Assignment(
                task_id=task.id, member_id=m.id,
                assigned_date=day, week_start=wk_start))

        for i, m in enumerate(members):
            week_effort[m.id] += day_counts[i] * task.effort

    # ── Every-2-days tasks: assigned on alternating remaining days ────────────
    every2_tasks = sorted(
        Task.query.filter_by(recurrence='every2days', active=True, home_id=hid).all(),
        key=lambda t: t.effort, reverse=True
    )
    every_other = remaining[::2]   # days 0, 2, 4, …
    n_e2 = len(every_other)

    for task in every2_tasks:
        if task.requires_two:
            for day in every_other:
                for m in members:
                    week_effort[m.id]         += task.effort
                    per_day_effort[day][m.id] += task.effort
                    db.session.add(Assignment(
                        task_id=task.id, member_id=m.id,
                        assigned_date=day, week_start=wk_start))
            continue

        if n_e2 == 0:
            continue

        best_offset, best_score = 0, float('inf')
        for offset in range(n_members):
            day_counts = [0] * n_members
            for d in range(n_e2):
                day_counts[(d + offset) % n_members] += 1

            projected_week = {
                m.id: week_effort[m.id] + day_counts[i] * task.effort
                for i, m in enumerate(members)
            }
            weekly_imbalance = max(projected_week.values()) - min(projected_week.values())

            daily_imbalance = 0
            for d_idx, day in enumerate(every_other):
                projected_day = dict(per_day_effort[day])
                projected_day[members[(d_idx + offset) % n_members].id] += task.effort
                daily_imbalance += max(projected_day.values()) - min(projected_day.values())

            score = weekly_imbalance * n_e2 + daily_imbalance
            if score < best_score:
                best_score  = score
                best_offset = offset

        day_counts = [0] * n_members
        for d_idx, day in enumerate(every_other):
            idx = (d_idx + best_offset) % n_members
            m   = members[idx]
            day_counts[idx] += 1
            per_day_effort[day][m.id] += task.effort
            db.session.add(Assignment(
                task_id=task.id, member_id=m.id,
                assigned_date=day, week_start=wk_start))

        for i, m in enumerate(members):
            week_effort[m.id] += day_counts[i] * task.effort

    db.session.flush()

    # Build per-member per-day effort map from daily assignments
    day_effort = {d: {m.id: 0 for m in members} for d in remaining}
    for day in remaining:
        for m in members:
            val = (db.session.query(db.func.sum(Task.effort))
                   .join(Assignment, Task.id == Assignment.task_id)
                   .filter(Assignment.assigned_date == day,
                           Assignment.member_id == m.id,
                           Task.home_id == hid)
                   .scalar()) or 0
            day_effort[day][m.id] = val

    # ── Weekly + ad-hoc ───────────────────────────────────────────────────────
    def distribute_once(tasks):
        wk_eff = {m.id: 0 for m in members}
        n = len(remaining)
        for task in sorted(tasks, key=priority_score, reverse=True):
            if task.requires_two:
                best_day = max(
                    range(n),
                    key=lambda i: (task.importance / 5.0) * (n - i)
                               - max(day_effort[remaining[i]][m.id] for m in members)
                )
                best_day = remaining[best_day]
                for m in members:
                    day_effort[best_day][m.id] += task.effort
                    wk_eff[m.id] += task.effort
                    db.session.add(Assignment(
                        task_id=task.id, member_id=m.id,
                        assigned_date=best_day, week_start=wk_start))
            else:
                best_score  = float('-inf')
                best_member = None
                best_day    = None
                for m in members:
                    for i, day in enumerate(remaining):
                        earliness    = (task.importance / 5.0) * (n - i)
                        day_penalty  = day_effort[day][m.id]
                        week_penalty = wk_eff[m.id] * 0.4
                        score = earliness - day_penalty - week_penalty
                        if score > best_score:
                            best_score  = score
                            best_member = m
                            best_day    = day
                day_effort[best_day][best_member.id] += task.effort
                wk_eff[best_member.id] += task.effort
                db.session.add(Assignment(
                    task_id=task.id, member_id=best_member.id,
                    assigned_date=best_day, week_start=wk_start))

    weekly_tasks = Task.query.filter_by(recurrence='weekly', active=True, home_id=hid).all()
    adhoc_tasks  = Task.query.filter_by(recurrence='adhoc',  active=True, home_id=hid).all()
    distribute_once(weekly_tasks + adhoc_tasks)

    # ── Bi-weekly + monthly ───────────────────────────────────────────────────
    def assign_member_only(tasks):
        effort_tracker = {m.id: 0 for m in members}
        for task in sorted(tasks, key=priority_score, reverse=True):
            if task.requires_two:
                for m in members:
                    db.session.add(Assignment(
                        task_id=task.id, member_id=m.id,
                        assigned_date=wk_start, week_start=wk_start))
            else:
                best = min(members, key=lambda m: effort_tracker[m.id])
                effort_tracker[best.id] += task.effort
                db.session.add(Assignment(
                    task_id=task.id, member_id=best.id,
                    assigned_date=wk_start, week_start=wk_start))

    biweekly_tasks = Task.query.filter_by(recurrence='biweekly', active=True, home_id=hid).all()
    monthly_tasks  = Task.query.filter_by(recurrence='monthly',  active=True, home_id=hid).all()
    assign_member_only(biweekly_tasks + monthly_tasks)

    db.session.commit()
    return True, f"Plan generated for {len(remaining)} day(s) — {wk_start.strftime('%b %d')} week."


# ── Home selector ──────────────────────────────────────────────────────────────

@app.route('/')
def home_selector():
    homes = Home.query.order_by(Home.id).all()
    if len(homes) == 1:
        return redirect(url_for('index', hid=homes[0].id))
    return render_template('homes.html')


@app.route('/homes/add', methods=['POST'])
def add_home():
    name = request.form.get('name', '').strip()
    icon = request.form.get('icon', '🏠').strip() or '🏠'
    if name:
        db.session.add(Home(name=name, icon=icon))
        db.session.commit()
        flash(f'Home "{name}" created.', 'success')
    return redirect(url_for('home_selector'))


@app.route('/homes/<int:hid>/edit', methods=['POST'])
def edit_home(hid):
    h = db.get_or_404(Home, hid)
    h.name = request.form.get('name', h.name).strip() or h.name
    h.icon = request.form.get('icon', h.icon).strip() or h.icon
    db.session.commit()
    flash(f'Home "{h.name}" updated.', 'success')
    return redirect(url_for('home_selector'))


@app.route('/homes/<int:hid>/delete', methods=['POST'])
def delete_home(hid):
    if Home.query.count() <= 1:
        flash('Cannot delete the last home.', 'danger')
        return redirect(url_for('home_selector'))
    h = db.get_or_404(Home, hid)
    Member.query.filter_by(home_id=hid).update({'active': False})
    Task.query.filter_by(home_id=hid).update({'active': False})
    db.session.delete(h)
    db.session.commit()
    flash(f'Home "{h.name}" deleted.', 'warning')
    return redirect(url_for('home_selector'))


# ── Plan view ──────────────────────────────────────────────────────────────────

@app.route('/h/<int:hid>/')
def index(hid):
    home     = db.get_or_404(Home, hid)
    today    = date.today()
    rem_days = remaining_week_days(today)
    wk_start = week_start_of(today)
    members  = Member.query.filter_by(active=True, home_id=hid).order_by(Member.id).all()

    daily_grid = {}
    for day in rem_days:
        daily_grid[day] = {m.id: [] for m in members}
        for a in (Assignment.query
                  .filter_by(assigned_date=day)
                  .join(Task)
                  .filter(Task.recurrence.in_(['daily', 'every2days', 'weekly', 'adhoc']),
                          Task.home_id == hid)
                  .order_by(Task.importance.desc())
                  .all()):
            if a.member_id in daily_grid[day]:
                daily_grid[day][a.member_id].append(a)

    biweekly = (Assignment.query
                .filter_by(week_start=wk_start)
                .join(Task).filter(Task.recurrence == 'biweekly', Task.home_id == hid).all())
    monthly  = (Assignment.query
                .filter_by(week_start=wk_start)
                .join(Task).filter(Task.recurrence == 'monthly', Task.home_id == hid).all())

    plan_exists = (Assignment.query
                   .filter(Assignment.assigned_date.in_(rem_days))
                   .join(Task).filter(Task.home_id == hid)
                   .first() is not None)

    return render_template('index.html',
        home=home, hid=hid, is_next_week=False,
        today=today, remaining_days=rem_days, members=members,
        daily_grid=daily_grid, biweekly_assignments=biweekly,
        monthly_assignments=monthly, plan_exists=plan_exists)


@app.route('/h/<int:hid>/generate', methods=['POST'])
def generate(hid):
    ok, msg = generate_plan(hid)
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('index', hid=hid))


@app.route('/h/<int:hid>/next')
def index_next(hid):
    home     = db.get_or_404(Home, hid)
    today    = date.today()
    wk_start = week_start_of(today) + timedelta(weeks=1)
    rem_days = [wk_start + timedelta(days=i) for i in range(7)]
    members  = Member.query.filter_by(active=True, home_id=hid).order_by(Member.id).all()

    daily_grid = {}
    for day in rem_days:
        daily_grid[day] = {m.id: [] for m in members}
        for a in (Assignment.query
                  .filter_by(assigned_date=day)
                  .join(Task)
                  .filter(Task.recurrence.in_(['daily', 'every2days', 'weekly', 'adhoc']),
                          Task.home_id == hid)
                  .order_by(Task.importance.desc())
                  .all()):
            if a.member_id in daily_grid[day]:
                daily_grid[day][a.member_id].append(a)

    biweekly = (Assignment.query
                .filter_by(week_start=wk_start)
                .join(Task).filter(Task.recurrence == 'biweekly', Task.home_id == hid).all())
    monthly  = (Assignment.query
                .filter_by(week_start=wk_start)
                .join(Task).filter(Task.recurrence == 'monthly', Task.home_id == hid).all())

    plan_exists = (Assignment.query
                   .filter(Assignment.assigned_date.in_(rem_days))
                   .join(Task).filter(Task.home_id == hid)
                   .first() is not None)

    return render_template('index.html',
        home=home, hid=hid, is_next_week=True,
        today=today, remaining_days=rem_days, members=members,
        daily_grid=daily_grid, biweekly_assignments=biweekly,
        monthly_assignments=monthly, plan_exists=plan_exists)


@app.route('/h/<int:hid>/generate/next', methods=['POST'])
def generate_next(hid):
    ok, msg = generate_plan(hid, for_next_week=True)
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('index_next', hid=hid))


@app.route('/h/<int:hid>/toggle/<int:aid>', methods=['POST'])
def toggle(hid, aid):
    a = db.get_or_404(Assignment, aid)
    a.completed = not a.completed
    db.session.commit()
    return redirect(request.referrer or url_for('index', hid=hid))


# ── Tasks CRUD ─────────────────────────────────────────────────────────────────

@app.route('/h/<int:hid>/tasks')
def tasks(hid):
    home = db.get_or_404(Home, hid)
    order = {'daily': 0, 'every2days': 1, 'weekly': 2, 'adhoc': 3, 'biweekly': 4, 'monthly': 5}
    all_tasks = sorted(
        Task.query.filter_by(active=True, home_id=hid).all(),
        key=lambda t: (order.get(t.recurrence, 9), -t.importance))
    categories = Category.query.filter_by(home_id=hid).order_by(Category.name).all()
    return render_template('tasks.html', home=home, hid=hid,
                           tasks=all_tasks, categories=categories)


@app.route('/h/<int:hid>/tasks/add', methods=['POST'])
def add_task(hid):
    t = Task(
        name         = request.form['name'].strip(),
        importance   = int(request.form.get('importance', 3)),
        effort       = int(request.form.get('effort', 3)),
        recurrence   = request.form.get('recurrence', 'weekly'),
        category     = request.form.get('category', '').strip(),
        requires_two = bool(request.form.get('requires_two')),
        home_id      = hid,
    )
    db.session.add(t)
    db.session.commit()
    flash(f'Task "{t.name}" added.', 'success')
    return redirect(url_for('tasks', hid=hid))


@app.route('/h/<int:hid>/tasks/<int:tid>/edit', methods=['POST'])
def edit_task(hid, tid):
    t = db.get_or_404(Task, tid)
    t.name         = request.form['name'].strip()
    t.importance   = int(request.form.get('importance', 3))
    t.effort       = int(request.form.get('effort', 3))
    t.recurrence   = request.form.get('recurrence', 'weekly')
    t.category     = request.form.get('category', '').strip()
    t.requires_two = bool(request.form.get('requires_two'))
    db.session.commit()
    flash(f'Task "{t.name}" updated.', 'success')
    return redirect(url_for('tasks', hid=hid))


@app.route('/h/<int:hid>/tasks/<int:tid>/delete', methods=['POST'])
def delete_task(hid, tid):
    t = db.get_or_404(Task, tid)
    t.active = False
    db.session.commit()
    flash(f'Task "{t.name}" removed.', 'warning')
    return redirect(url_for('tasks', hid=hid))


# ── Categories CRUD ────────────────────────────────────────────────────────────

@app.route('/h/<int:hid>/categories/add', methods=['POST'])
def add_category(hid):
    name = request.form.get('name', '').strip()
    if name and not Category.query.filter_by(name=name, home_id=hid).first():
        db.session.add(Category(name=name, home_id=hid))
        db.session.commit()
        flash(f'Category "{name}" added.', 'success')
    return redirect(url_for('tasks', hid=hid))


@app.route('/h/<int:hid>/categories/<int:cid>/delete', methods=['POST'])
def delete_category(hid, cid):
    c = db.get_or_404(Category, cid)
    db.session.delete(c)
    db.session.commit()
    flash(f'Category "{c.name}" removed.', 'warning')
    return redirect(url_for('tasks', hid=hid))


# ── Members CRUD ───────────────────────────────────────────────────────────────

@app.route('/h/<int:hid>/members')
def members(hid):
    home = db.get_or_404(Home, hid)
    return render_template('members.html', home=home, hid=hid,
        members=Member.query.filter_by(active=True, home_id=hid).order_by(Member.id).all())


@app.route('/h/<int:hid>/members/add', methods=['POST'])
def add_member(hid):
    m = Member(name=request.form['name'].strip(),
               color=request.form.get('color', '#4A90E2'),
               home_id=hid)
    db.session.add(m)
    db.session.commit()
    flash(f'Member "{m.name}" added.', 'success')
    return redirect(url_for('members', hid=hid))


@app.route('/h/<int:hid>/members/<int:mid>/edit', methods=['POST'])
def edit_member(hid, mid):
    m = db.get_or_404(Member, mid)
    m.name  = request.form['name'].strip()
    m.color = request.form.get('color', m.color)
    db.session.commit()
    flash(f'Member "{m.name}" updated.', 'success')
    return redirect(url_for('members', hid=hid))


@app.route('/h/<int:hid>/members/<int:mid>/delete', methods=['POST'])
def delete_member(hid, mid):
    m = db.get_or_404(Member, mid)
    m.active = False
    db.session.commit()
    flash(f'Member "{m.name}" removed.', 'warning')
    return redirect(url_for('members', hid=hid))


# ── Print helpers ──────────────────────────────────────────────────────────────

def _print_data(hid, for_next_week=False):
    from collections import OrderedDict
    today = date.today()
    if for_next_week:
        wk_start = week_start_of(today) + timedelta(weeks=1)
        rem_days = [wk_start + timedelta(days=i) for i in range(7)]
    else:
        wk_start = week_start_of(today)
        rem_days = remaining_week_days(today)
    members  = Member.query.filter_by(active=True, home_id=hid).order_by(Member.id).all()

    task_day_map = {}
    task_objects = {}
    for day in rem_days:
        for a in (Assignment.query
                  .filter_by(assigned_date=day)
                  .join(Task)
                  .filter(Task.recurrence.in_(['daily', 'every2days', 'weekly', 'adhoc']),
                          Task.home_id == hid)
                  .all()):
            if a.task_id not in task_day_map:
                task_day_map[a.task_id] = {}
                task_objects[a.task_id] = a.task
            task_day_map[a.task_id].setdefault(day, []).append(a)

    sorted_tasks = sorted(
        task_objects.values(),
        key=lambda t: (t.category or '\xff', -t.importance, t.effort)
    )

    cat_groups = OrderedDict()
    for task in sorted_tasks:
        if task.id not in task_day_map:
            continue
        cat = task.category or ''
        if cat not in cat_groups:
            cat_groups[cat] = {'daily': [], 'weekly': []}
        bucket = 'daily' if task.recurrence in ('daily', 'every2days') else 'weekly'
        cat_groups[cat][bucket].append(task)
    cat_groups = OrderedDict(
        sorted(
            ((k, v) for k, v in cat_groups.items() if v['daily'] or v['weekly']),
            key=lambda kv: len(kv[1]['daily']) + len(kv[1]['weekly']),
            reverse=True,
        )
    )

    biweekly = (Assignment.query.filter_by(week_start=wk_start)
                .join(Task).filter(Task.recurrence == 'biweekly', Task.home_id == hid).all())
    monthly  = (Assignment.query.filter_by(week_start=wk_start)
                .join(Task).filter(Task.recurrence == 'monthly', Task.home_id == hid).all())

    COMBINE_THRESHOLD = 3
    groups_list = list(cat_groups.items())
    large = [(k, v) for k, v in groups_list if len(v['daily']) >= COMBINE_THRESHOLD]
    small = [(k, v) for k, v in groups_list if len(v['daily']) < COMBINE_THRESHOLD]

    display_groups = []
    for k, v in large:
        display_groups.append({
            'labels': [k or 'General'],
            'daily':  v['daily'],
            'weekly': v['weekly'],
        })
    for i in range(0, len(small), 2):
        pair = small[i:i + 2]
        display_groups.append({
            'labels': [k or 'General' for k, v in pair],
            'daily':  [t for k, v in pair for t in v['daily']],
            'weekly': [t for k, v in pair for t in v['weekly']],
        })
    display_groups.sort(
        key=lambda g: len(g['daily']) + len(g['weekly']), reverse=True)

    return dict(hid=hid, is_next_week=for_next_week, today=today, remaining_days=rem_days, members=members,
                sorted_tasks=sorted_tasks, task_day_map=task_day_map,
                cat_groups=cat_groups, display_groups=display_groups,
                biweekly_assignments=biweekly, monthly_assignments=monthly)


@app.route('/h/<int:hid>/print')
def print_view(hid):
    return render_template('print.html', **_print_data(hid))


@app.route('/h/<int:hid>/print2')
def print_view2(hid):
    return render_template('print2.html', **_print_data(hid))


@app.route('/h/<int:hid>/next/print')
def print_view_next(hid):
    return render_template('print.html', **_print_data(hid, for_next_week=True))


@app.route('/h/<int:hid>/next/print2')
def print_view2_next(hid):
    return render_template('print2.html', **_print_data(hid, for_next_week=True))


# ── Bootstrap ──────────────────────────────────────────────────────────────────

def migrate():
    """Add new columns to existing DB without losing data."""
    # Legacy: requires_two on task
    try:
        db.session.execute(text(
            'ALTER TABLE task ADD COLUMN requires_two BOOLEAN NOT NULL DEFAULT 0'))
        db.session.commit()
    except Exception:
        db.session.rollback()

    # home_id on member and task
    for tbl in ['member', 'task']:
        try:
            db.session.execute(text(
                f'ALTER TABLE {tbl} ADD COLUMN home_id INTEGER NOT NULL DEFAULT 1'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    # category: add home_id + drop unique(name) constraint (SQLite workaround)
    cols = [r[1] for r in db.session.execute(
        text('PRAGMA table_info(category)')).fetchall()]
    if 'home_id' not in cols:
        try:
            db.session.execute(text(
                'ALTER TABLE category ADD COLUMN home_id INTEGER NOT NULL DEFAULT 1'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    try:
        tbl_sql = db.session.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='category'")
        ).scalar() or ''
        if 'UNIQUE' in tbl_sql.upper():
            db.session.execute(text(
                'CREATE TABLE _cat_new '
                '(id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL, '
                'home_id INTEGER NOT NULL DEFAULT 1)'))
            db.session.execute(text(
                'INSERT INTO _cat_new SELECT id, name, home_id FROM category'))
            db.session.execute(text('DROP TABLE category'))
            db.session.execute(text('ALTER TABLE _cat_new RENAME TO category'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def seed_defaults():
    # Ensure at least one Home row exists (for existing DBs migrating from v1)
    if Home.query.count() == 0:
        h1 = Home(name='My Home', icon='🏠')
        h2 = Home(name="Bro's Place", icon='🏡')
        db.session.add_all([h1, h2])
        db.session.flush()
        home1_id = h1.id
        home2_id = h2.id
    else:
        home1_id = Home.query.order_by(Home.id).first().id
        home2_id = None
        if Home.query.count() < 2:
            h2 = Home(name="Bro's Place", icon='🏡')
            db.session.add(h2)
            db.session.flush()
            home2_id = h2.id

    # Seed home 1 members/categories/tasks if absent
    if Member.query.filter_by(home_id=home1_id).count() == 0:
        db.session.add_all([
            Member(name='Alex', color='#4A90E2', home_id=home1_id),
            Member(name='Sam',  color='#E2704A', home_id=home1_id),
        ])
    if Category.query.filter_by(home_id=home1_id).count() == 0:
        for name in ['Kitchen', 'Cleaning', 'Laundry', 'Shopping',
                     'Garden', 'Bedroom', 'Bathroom', 'General']:
            db.session.add(Category(name=name, home_id=home1_id))
    if Task.query.filter_by(home_id=home1_id).count() == 0:
        db.session.add_all([
            Task(name='Wash dishes',        importance=4, effort=1, recurrence='daily',    category='Kitchen',  home_id=home1_id),
            Task(name='Cook dinner',        importance=5, effort=3, recurrence='daily',    category='Kitchen',  home_id=home1_id),
            Task(name='Tidy living room',   importance=3, effort=1, recurrence='daily',    category='Cleaning', home_id=home1_id),
            Task(name='Vacuum floors',      importance=3, effort=2, recurrence='weekly',   category='Cleaning', home_id=home1_id),
            Task(name='Laundry',            importance=4, effort=2, recurrence='weekly',   category='Laundry',  home_id=home1_id),
            Task(name='Grocery shopping',   importance=5, effort=3, recurrence='weekly',   category='Shopping', home_id=home1_id),
            Task(name='Take out bins',      importance=4, effort=1, recurrence='weekly',   category='General',  home_id=home1_id),
            Task(name='Clean bathroom',     importance=4, effort=3, recurrence='biweekly', category='Bathroom', home_id=home1_id),
            Task(name='Change bed sheets',  importance=3, effort=2, recurrence='biweekly', category='Bedroom',  home_id=home1_id),
            Task(name='Mop floors',         importance=3, effort=3, recurrence='biweekly', category='Cleaning', home_id=home1_id),
            Task(name='Deep clean kitchen', importance=4, effort=4, recurrence='monthly',  category='Kitchen',  home_id=home1_id),
            Task(name='Window cleaning',    importance=2, effort=3, recurrence='monthly',  category='Cleaning', home_id=home1_id),
        ])

    # Seed home 2 if newly created
    if home2_id:
        if Member.query.filter_by(home_id=home2_id).count() == 0:
            db.session.add(Member(name='Bro', color='#9B59B6', home_id=home2_id))
        if Category.query.filter_by(home_id=home2_id).count() == 0:
            for name in ['Kitchen', 'Cleaning', 'Laundry', 'Shopping',
                         'Garden', 'Bedroom', 'Bathroom', 'General']:
                db.session.add(Category(name=name, home_id=home2_id))

    db.session.commit()


with app.app_context():
    db.create_all()
    migrate()
    seed_defaults()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=2026, debug=True)
