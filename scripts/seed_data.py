"""Database Seeding Script for Interactive Manual Testing (Phase 1 & Phase 2).

Creates:
- 1 Active Tenant ("Gourmet Dining Group")
- 1 Branch ("Downtown Branch", lat=24.7136, lon=46.6753, geofence=150m)
- 2 Tables ("T-01" and "T-02")
- 3 Staff Users (All with password 'Admin123!'):
    * Super Admin (admin@gourmet.com)
    * Branch Admin (branchadmin@gourmet.com) -> Allowed Branch
    * Cashier (cashier@gourmet.com) -> Allowed Branch
- 1 Category: "Burgers & Mains" linked to KitchenStation.HOT_KITCHEN
- 1 Item: "Classic Smash Burger" (bilingual: en & ar, base_price=35.00 SAR, HOT_KITCHEN)
- 1 Modifier Group: "Size" (min_choices=1, max_choices=1, is_required=True)
- 3 Modifier Options:
    * "Single Patty" (price_delta=0.00)
    * "Double Patty" (price_delta=10.00)
    * "Triple Patty" (price_delta=18.00)
- 1 Second Item (Item 86 demonstration): "Truffle Fries" (is_available=False, base_price=22.00 SAR)

Run with:
    python scripts/seed_data.py
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.core.qr_security import QRSignatureEngine
from app.core.security import get_password_hash
from app.models.auth import Branch, Table, Tenant, User, UserBranchAccess
from app.models.catalog import Category, Item, ModifierGroup, ModifierOption
from app.models.enums import KitchenStation, TableStatus, UserRole


async def seed_database() -> dict[str, str]:
    async with async_session_factory() as session:
        # Check if already seeded
        existing_tenant_stmt = select(Tenant).where(Tenant.slug == "gourmet-dining")
        existing_tenant = (await session.execute(existing_tenant_stmt)).scalar_one_or_none()

        if existing_tenant:
            print("Database already contains seed data for slug 'gourmet-dining'.")
            # Fetch IDs for quick display
            tenant = existing_tenant
            branch_stmt = select(Branch).where(Branch.tenant_id == tenant.id)
            branch = (await session.execute(branch_stmt)).scalars().first()
            table_stmt = select(Table).where(Table.branch_id == branch.id, Table.table_number == "T-01")
            table = (await session.execute(table_stmt)).scalar_one_or_none()
            item_stmt = select(Item).join(Category).where(Category.branch_id == branch.id)
            item = (await session.execute(item_stmt)).scalars().first()
            mod_group_stmt = select(ModifierGroup).where(ModifierGroup.item_id == item.id)
            mod_group = (await session.execute(mod_group_stmt)).scalar_one_or_none()
            mod_opt_stmt = select(ModifierOption).where(ModifierOption.modifier_group_id == mod_group.id)
            mod_opts = (await session.execute(mod_opt_stmt)).scalars().all()

            qr_token = QRSignatureEngine.sign_table_token(tenant.id, branch.id, table.id)
            return {
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "table_id": str(table.id),
                "item_id": str(item.id),
                "modifier_group_id": str(mod_group.id),
                "option_single_id": str(mod_opts[0].id) if mod_opts else "",
                "option_double_id": str(mod_opts[1].id) if len(mod_opts) > 1 else "",
                "qr_token": qr_token,
            }

        # 1. Tenant
        tenant = Tenant(
            id=uuid.uuid4(),
            name="Gourmet Dining Group",
            slug="gourmet-dining",
            is_active=True,
        )
        session.add(tenant)
        await session.flush()

        # 2. Branch
        branch = Branch(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name={"en": "Downtown Branch", "ar": "فرع وسط المدينة"},
            slug="downtown",
            latitude=Decimal("24.7135517"),
            longitude=Decimal("46.6752957"),
            geofence_radius_meters=150,
            is_active=True,
        )
        session.add(branch)
        await session.flush()

        # 3. Tables
        table_1 = Table(
            id=uuid.uuid4(),
            branch_id=branch.id,
            table_number="T-01",
            capacity=4,
            status=TableStatus.AVAILABLE,
            is_active=True,
        )
        table_2 = Table(
            id=uuid.uuid4(),
            branch_id=branch.id,
            table_number="T-02",
            capacity=2,
            status=TableStatus.AVAILABLE,
            is_active=True,
        )
        session.add_all([table_1, table_2])
        await session.flush()

        # 4. Staff Users (Password: Admin123!)
        pw_hash = get_password_hash("Admin123!")

        super_admin = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email="admin@gourmet.com",
            hashed_password=pw_hash,
            full_name="Alice (Super Admin)",
            role=UserRole.SUPER_ADMIN,
            is_active=True,
        )
        branch_admin = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email="branchadmin@gourmet.com",
            hashed_password=pw_hash,
            full_name="Bob (Branch Admin)",
            role=UserRole.BRANCH_ADMIN,
            is_active=True,
        )
        cashier = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email="cashier@gourmet.com",
            hashed_password=pw_hash,
            full_name="Charlie (Cashier)",
            role=UserRole.CASHIER,
            is_active=True,
        )
        session.add_all([super_admin, branch_admin, cashier])
        await session.flush()

        # Branch access assignments
        ba_admin = UserBranchAccess(user_id=branch_admin.id, branch_id=branch.id)
        ba_cashier = UserBranchAccess(user_id=cashier.id, branch_id=branch.id)
        session.add_all([ba_admin, ba_cashier])
        await session.flush()

        # 5. Category (HOT_KITCHEN)
        category = Category(
            id=uuid.uuid4(),
            branch_id=branch.id,
            name={"en": "Burgers & Mains", "ar": "البرغر والأطباق الرئيسية"},
            display_order=1,
            station=KitchenStation.HOT_KITCHEN,
            is_active=True,
        )
        session.add(category)
        await session.flush()

        # 6. Items
        # Item 1: Classic Smash Burger (Available)
        burger = Item(
            id=uuid.uuid4(),
            category_id=category.id,
            name={"en": "Classic Smash Burger", "ar": "كلاسيك سماش برغر"},
            description={
                "en": "Prime Angus beef patty, melted cheddar, house sauce, toasted brioche bun",
                "ar": "شريحة لحم أنغوس فاخرة، جبنة شيدر ذائبة، صوص خاص، خبز بريوش محمص",
            },
            base_price=Decimal("35.00"),
            station=KitchenStation.HOT_KITCHEN,
            is_available=True,
            allergens=["gluten", "dairy"],
            dietary_badges=["halal"],
        )

        # Item 2: Truffle Fries (86'd - Unavailable for testing Item 86 switch)
        truffle_fries = Item(
            id=uuid.uuid4(),
            category_id=category.id,
            name={"en": "Truffle Fries", "ar": "بطاطس الترفل"},
            description={
                "en": "Crispy hand-cut fries with parmesan and black truffle oil",
                "ar": "بطاطس مقرمشة مقطعة يدوياً مع بارميزان وزيت الترفل الأسود",
            },
            base_price=Decimal("22.00"),
            station=KitchenStation.HOT_KITCHEN,
            is_available=False,  # 86 switch turned ON!
            allergens=["dairy"],
            dietary_badges=["vegetarian"],
        )
        session.add_all([burger, truffle_fries])
        await session.flush()

        # 7. Modifier Group for Burger
        size_group = ModifierGroup(
            id=uuid.uuid4(),
            item_id=burger.id,
            name={"en": "Patty Size", "ar": "حجم الشريحة"},
            min_choices=1,
            max_choices=1,
            is_required=True,
        )
        session.add(size_group)
        await session.flush()

        # 8. Modifier Options with price adjustments
        opt_single = ModifierOption(
            id=uuid.uuid4(),
            modifier_group_id=size_group.id,
            name={"en": "Single Patty", "ar": "شريحة واحدة"},
            price_delta=Decimal("0.00"),
            is_available=True,
        )
        opt_double = ModifierOption(
            id=uuid.uuid4(),
            modifier_group_id=size_group.id,
            name={"en": "Double Patty (+10 SAR)", "ar": "شريحتان (+10 ر.س)"},
            price_delta=Decimal("10.00"),
            is_available=True,
        )
        opt_triple = ModifierOption(
            id=uuid.uuid4(),
            modifier_group_id=size_group.id,
            name={"en": "Triple Patty (+18 SAR)", "ar": "ثلاث شرائح (+18 ر.س)"},
            price_delta=Decimal("18.00"),
            is_available=True,
        )
        session.add_all([opt_single, opt_double, opt_triple])

        await session.commit()

        # Sign QR token for Table 1
        qr_token = QRSignatureEngine.sign_table_token(tenant.id, branch.id, table_1.id)

        print("\n==================================================================")
        print("🎉 SEED DATA CREATED SUCCESSFULLY!")
        print("==================================================================")
        print(f"Tenant ID:         {tenant.id}")
        print(f"Branch ID:         {branch.id} (Lat: 24.7135517, Lon: 46.6752957)")
        print(f"Table 1 ID (T-01): {table_1.id}")
        print(f"Table 2 ID (T-02): {table_2.id}")
        print("Staff Users (Password for all: 'Admin123!'):")
        print("  - Super Admin:   admin@gourmet.com")
        print("  - Branch Admin:  branchadmin@gourmet.com")
        print("  - Cashier:       cashier@gourmet.com")
        print(f"Category ID:       {category.id}")
        print(f"Item ID (Burger):  {burger.id} (Base Price: 35.00 SAR)")
        print(f"Item ID (Fries):   {truffle_fries.id} (Unavailable - 86'd)")
        print(f"Modifier Group:    {size_group.id} ('Patty Size', min=1, max=1)")
        print(f"  * Single Option: {opt_single.id} (+0.00 SAR)")
        print(f"  * Double Option: {opt_double.id} (+10.00 SAR)")
        print(f"  * Triple Option: {opt_triple.id} (+18.00 SAR)")
        print(f"\n🔑 Valid HMAC Table 1 QR Token:\n{qr_token}")
        print("==================================================================\n")

        return {
            "tenant_id": str(tenant.id),
            "branch_id": str(branch.id),
            "table_id": str(table_1.id),
            "item_id": str(burger.id),
            "modifier_group_id": str(size_group.id),
            "option_single_id": str(opt_single.id),
            "option_double_id": str(opt_double.id),
            "qr_token": qr_token,
        }


if __name__ == "__main__":
    asyncio.run(seed_database())
