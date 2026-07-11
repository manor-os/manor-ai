import pytest

from packages.core.models.base import generate_ulid


@pytest.mark.asyncio
async def test_task_retried_event_notifies_involved_users():
    import packages.core.database as dbmod
    from packages.core.models.notification import Notification
    from packages.core.models.task import Task
    from packages.core.models.user import Entity, User
    from packages.core.services.task_event_notifications import notify_task_event
    from sqlalchemy import select

    entity_id = generate_ulid()
    requester_id = generate_ulid()
    creator_id = generate_ulid()
    assignee_id = generate_ulid()
    task_id = generate_ulid()

    async with dbmod.async_session() as db:
        db.add(Entity(id=entity_id, name="Task Event Corp"))
        db.add_all(
            [
                User(
                    id=requester_id,
                    entity_id=entity_id,
                    email=f"{requester_id}@test.com",
                    display_name="Requester",
                    password_hash="x",
                    role="admin",
                    status="active",
                ),
                User(
                    id=creator_id,
                    entity_id=entity_id,
                    email=f"{creator_id}@test.com",
                    display_name="Creator",
                    password_hash="x",
                    role="member",
                    status="active",
                ),
                User(
                    id=assignee_id,
                    entity_id=entity_id,
                    email=f"{assignee_id}@test.com",
                    display_name="Assignee",
                    password_hash="x",
                    role="member",
                    status="active",
                ),
            ]
        )
        db.add(
            Task(
                id=task_id,
                entity_id=entity_id,
                title="Repair workflow",
                status="in_progress",
                priority=3,
                task_type="general",
                creator_id=creator_id,
                assignee_id=assignee_id,
            )
        )
        await db.commit()

        delivered = await notify_task_event(
            db,
            entity_id,
            "task.retried",
            {
                "task_id": task_id,
                "requested_by": requester_id,
                "mode": "plan",
                "plan_id": "plan-1",
                "step_ids": ["step-1"],
                "reset_steps": 1,
            },
        )
        await db.commit()

        assert delivered == 3
        rows = list((await db.execute(select(Notification).where(Notification.entity_id == entity_id))).scalars().all())
        assert {n.user_id for n in rows} == {requester_id, creator_id, assignee_id}
        assert {n.type for n in rows} == {"task_retried"}
        assert all(n.meta["event_type"] == "task.retried" for n in rows)
        assert all(n.meta["step_ids"] == ["step-1"] for n in rows)
        assert all(n.meta["link"] == f"/tasks/{task_id}" for n in rows)


@pytest.mark.asyncio
async def test_task_assigned_event_resolves_staff_assignee_to_login_user():
    import packages.core.database as dbmod
    from packages.core.models.notification import Notification
    from packages.core.models.staff import Staff
    from packages.core.models.task import Task
    from packages.core.models.user import Entity, User
    from packages.core.services.task_event_notifications import notify_task_event
    from sqlalchemy import select

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    assignee_user_id = generate_ulid()
    staff_id = generate_ulid()
    task_id = generate_ulid()

    async with dbmod.async_session() as db:
        db.add(Entity(id=entity_id, name="Staff Notify Corp"))
        db.add_all([
            User(
                id=creator_id,
                entity_id=entity_id,
                email=f"{creator_id}@test.com",
                display_name="Creator",
                password_hash="x",
                role="admin",
                status="active",
            ),
            User(
                id=assignee_user_id,
                entity_id=entity_id,
                email=f"{assignee_user_id}@test.com",
                display_name="Assignee Login",
                password_hash="x",
                role="member",
                status="active",
            ),
        ])
        await db.flush()
        db.add_all([
            Staff(
                id=staff_id,
                entity_id=entity_id,
                name="Assignee Staff",
                user_id=assignee_user_id,
                status="active",
            ),
            Task(
                id=task_id,
                entity_id=entity_id,
                title="Follow up with guest",
                status="pending",
                priority=3,
                task_type="general",
                creator_id=creator_id,
                assignee_id=staff_id,
            ),
        ])
        await db.commit()

        delivered = await notify_task_event(
            db,
            entity_id,
            "task.assigned",
            {
                "task_id": task_id,
                "creator_id": creator_id,
                "assignee_id": staff_id,
                "assigned_by": creator_id,
            },
        )
        await db.commit()

        assert delivered == 2
        rows = list((await db.execute(
            select(Notification).where(Notification.entity_id == entity_id)
        )).scalars().all())
        assert {n.user_id for n in rows} == {creator_id, assignee_user_id}
        assert {n.type for n in rows} == {"task_assigned"}


@pytest.mark.asyncio
async def test_task_status_event_resolves_staff_membership_assignee():
    import packages.core.database as dbmod
    from packages.core.models.notification import Notification
    from packages.core.models.staff import Staff
    from packages.core.models.task import Task
    from packages.core.models.user import Entity, User, UserMembership
    from packages.core.services.task_event_notifications import notify_task_event
    from sqlalchemy import select

    entity_id = generate_ulid()
    user_home_entity_id = generate_ulid()
    creator_id = generate_ulid()
    member_user_id = generate_ulid()
    staff_id = generate_ulid()
    task_id = generate_ulid()

    async with dbmod.async_session() as db:
        db.add_all([
            Entity(id=entity_id, name="Membership Notify Corp"),
            Entity(id=user_home_entity_id, name="User Home Corp"),
            User(
                id=creator_id,
                entity_id=entity_id,
                email=f"{creator_id}@test.com",
                display_name="Creator",
                password_hash="x",
                role="admin",
                status="active",
            ),
            User(
                id=member_user_id,
                entity_id=user_home_entity_id,
                email=f"{member_user_id}@test.com",
                display_name="Membership User",
                password_hash="x",
                role="member",
                status="active",
            ),
        ])
        await db.flush()
        db.add_all([
            UserMembership(
                id=generate_ulid(),
                user_id=member_user_id,
                entity_id=entity_id,
                role="member",
                status="active",
                staff_id=staff_id,
            ),
            Staff(
                id=staff_id,
                entity_id=entity_id,
                name="Membership Staff",
                status="active",
            ),
            Task(
                id=task_id,
                entity_id=entity_id,
                title="Prepare weekly update",
                status="in_progress",
                priority=3,
                task_type="general",
                creator_id=creator_id,
                assignee_id=staff_id,
            ),
        ])
        await db.commit()

        delivered = await notify_task_event(
            db,
            entity_id,
            "task.status_changed",
            {
                "task_id": task_id,
                "creator_id": creator_id,
                "assignee_id": staff_id,
                "changed_by": creator_id,
                "old_status": "pending",
                "new_status": "in_progress",
            },
        )
        await db.commit()

        assert delivered == 2
        rows = list((await db.execute(
            select(Notification).where(Notification.entity_id == entity_id)
        )).scalars().all())
        assert {n.user_id for n in rows} == {creator_id, member_user_id}
        assert {n.type for n in rows} == {"task_status_changed"}


@pytest.mark.asyncio
async def test_hitl_reminder_event_falls_back_to_entity_admins():
    import packages.core.database as dbmod
    from packages.core.models.notification import Notification
    from packages.core.models.task import Task
    from packages.core.models.user import Entity, User
    from packages.core.services.task_event_notifications import notify_task_event
    from sqlalchemy import select

    entity_id = generate_ulid()
    owner_id = generate_ulid()
    member_id = generate_ulid()
    task_id = generate_ulid()

    async with dbmod.async_session() as db:
        db.add(Entity(id=entity_id, name="Reminder Corp"))
        db.add_all(
            [
                User(
                    id=owner_id,
                    entity_id=entity_id,
                    email=f"{owner_id}@test.com",
                    display_name="Owner",
                    password_hash="x",
                    role="owner",
                    status="active",
                ),
                User(
                    id=member_id,
                    entity_id=entity_id,
                    email=f"{member_id}@test.com",
                    display_name="Member",
                    password_hash="x",
                    role="member",
                    status="active",
                ),
            ]
        )
        db.add(
            Task(
                id=task_id,
                entity_id=entity_id,
                title="Approve copy",
                status="waiting_on_customer",
                priority=3,
                task_type="general",
            )
        )
        await db.commit()

        delivered = await notify_task_event(
            db,
            entity_id,
            "task.hitl_reminder",
            {
                "task_id": task_id,
                "plan_id": "plan-1",
                "step_id": "step-1",
                "wait_minutes": 75,
                "prompt": "Pick option A or B.",
            },
        )
        await db.commit()

        assert delivered == 1
        rows = list((await db.execute(select(Notification).where(Notification.entity_id == entity_id))).scalars().all())
        assert len(rows) == 1
        notif = rows[0]
        assert notif.user_id == owner_id
        assert notif.type == "task_hitl_reminder"
        assert notif.title == "Input still needed"
        assert "75 minute" in (notif.content or "")
        assert "Pick option A or B." in (notif.content or "")
        assert notif.meta["event_type"] == "task.hitl_reminder"
        assert notif.meta["link"] == f"/tasks/{task_id}"


@pytest.mark.asyncio
async def test_hitl_requested_event_falls_back_to_entity_admins_for_ai_tasks():
    import packages.core.database as dbmod
    from packages.core.models.notification import Notification
    from packages.core.models.task import Task
    from packages.core.models.user import Entity, User
    from packages.core.services.task_event_notifications import notify_task_event
    from sqlalchemy import select

    entity_id = generate_ulid()
    owner_id = generate_ulid()
    member_id = generate_ulid()
    task_id = generate_ulid()

    async with dbmod.async_session() as db:
        db.add(Entity(id=entity_id, name="AI Task Corp"))
        db.add_all(
            [
                User(
                    id=owner_id,
                    entity_id=entity_id,
                    email=f"{owner_id}@test.com",
                    display_name="Owner",
                    password_hash="x",
                    role="owner",
                    status="active",
                ),
                User(
                    id=member_id,
                    entity_id=entity_id,
                    email=f"{member_id}@test.com",
                    display_name="Member",
                    password_hash="x",
                    role="member",
                    status="active",
                ),
            ]
        )
        db.add(
            Task(
                id=task_id,
                entity_id=entity_id,
                title="AI generated task needs missing artifact",
                status="waiting_on_customer",
                priority=3,
                task_type="general",
            )
        )
        await db.commit()

        delivered = await notify_task_event(
            db,
            entity_id,
            "task.hitl_requested",
            {
                "task_id": task_id,
                "plan_id": "plan-1",
                "prompt": "Please attach the final memo.",
            },
        )
        await db.commit()

        assert delivered == 1
        rows = list((await db.execute(select(Notification).where(Notification.entity_id == entity_id))).scalars().all())
        assert len(rows) == 1
        notif = rows[0]
        assert notif.user_id == owner_id
        assert notif.type == "task_hitl_requested"
        assert notif.title == "Input needed"
        assert "waiting for your input" in (notif.content or "")
        assert notif.meta["event_type"] == "task.hitl_requested"
        assert notif.meta["link"] == f"/tasks/{task_id}"
