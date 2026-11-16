# Minecraft Java Edition 1.21.11 原版进度（Advancements）全表

- **适用服务器**：`mc.nmo.net.cn` -> `survivaltest`（服务端 MOTD 版本 `1.21.11`，Java 21，官方发布时间 `2025-12-09`）
- **非配方进度总数**：**125 项**（另有 `minecraft:recipes/*` 配方进度 1459 项，API `POST /advancements` 默认 `include_recipes: false` 隐藏）
- **服务端下发机制注意**：原版服务端仅向客户端下发**已解锁根节点（或已解锁父节点后 2 层内）的可见进度子树**。新角色刚出生时 `POST /advancements` 返回 `total: 0` 属正常现象，Agent 规划须参考本完整目录（机器可读数据见 `src/mc_agent/data/advancements_1_21_11.json`）。

## 分类汇总

| 分类前缀 | 名称 | 总数 | Task (普通) | Goal (目标) | Challenge (挑战) | 根节点解锁条件 |
|---|---|---:|---:|---:|---:|---|
| `minecraft:story/*` | Minecraft（故事） | 16 | 15 | 1 | 0 | 获得工作台 (`crafting_table`) 后下发子树 |
| `minecraft:nether/*` | Nether（下界） | 23 | 15 | 1 | 7 | 进入下界维度 (`the_nether`) 后下发子树 |
| `minecraft:end/*` | The End（末地） | 9 | 4 | 4 | 1 | 进入末地维度 (`the_end`) 后下发子树 |
| `minecraft:adventure/*` | Adventure（冒险） | 47 | 32 | 4 | 11 | 击杀任意生物或被生物击杀后下发子树 |
| `minecraft:husbandry/*` | Husbandry（农牧业） | 30 | 24 | 0 | 6 | 消耗/吃下任意可食用物品后下发子树 |

## Minecraft（故事）（共 16 项）

| # | ID (`minecraft:...`) | 中文名 | 英文名 | 级别 (`frame`) | 前置 (`parent`) | 达成条件（官方描述） | 备注 |
|---:|---|---|---|---|---|---|---|
| 1 | `story/root` | Minecraft | Minecraft | `task` | `-` | 游戏的核心与故事 | - |
| 2 | `story/mine_stone` | 石器时代 | Stone Age | `task` | `story/root` | 用你的新镐挖掘石头 | - |
| 3 | `story/upgrade_tools` | 获得升级 | Getting an Upgrade | `task` | `story/mine_stone` | 制作一把更好的镐 | - |
| 4 | `story/smelt_iron` | 来硬的 | Acquire Hardware | `task` | `story/upgrade_tools` | 冶炼出一块铁锭 | - |
| 5 | `story/iron_tools` | 这不是铁镐么？ | Isn't It Iron Pick | `task` | `story/smelt_iron` | 升级你的镐 | - |
| 6 | `story/lava_bucket` | 热腾腾的 | Hot Stuff | `task` | `story/smelt_iron` | 用铁桶装点熔岩 | - |
| 7 | `story/obtain_armor` | 整装上阵 | Suit Up | `task` | `story/smelt_iron` | 用铁盔甲来保护你自己 | - |
| 8 | `story/deflect_arrow` | 不吃这套，谢谢 | Not Today, Thank You | `task` | `story/obtain_armor` | 用盾牌反弹一个弹射物 | - |
| 9 | `story/form_obsidian` | 冰桶挑战 | Ice Bucket Challenge | `task` | `story/lava_bucket` | 获得一块黑曜石 | - |
| 10 | `story/mine_diamond` | 钻石！ | Diamonds! | `task` | `story/iron_tools` | 获得钻石 | - |
| 11 | `story/enchant_item` | 附魔师 | Enchanter | `task` | `story/mine_diamond` | 用附魔台附魔一样物品 | - |
| 12 | `story/enter_the_nether` | 勇往直下 | We Need to Go Deeper | `task` | `story/form_obsidian` | 建造、激活并进入一座下界传送门 | - |
| 13 | `story/shiny_gear` | 钻石护体 | Cover Me with Diamonds | `task` | `story/mine_diamond` | 钻石盔甲能救人 | - |
| 14 | `story/cure_zombie_villager` | 僵尸科医生 | Zombie Doctor | `goal` | `story/enter_the_nether` | 弱化并治疗一名僵尸村民 | - |
| 15 | `story/follow_ender_eye` | 隔墙有眼 | Eye Spy | `task` | `story/enter_the_nether` | 跟随末影之眼 | - |
| 16 | `story/enter_the_end` | 结束了？ | The End? | `task` | `story/follow_ender_eye` | 进入末地传送门 | - |

## Nether（下界）（共 23 项）

| # | ID (`minecraft:...`) | 中文名 | 英文名 | 级别 (`frame`) | 前置 (`parent`) | 达成条件（官方描述） | 备注 |
|---:|---|---|---|---|---|---|---|
| 1 | `nether/root` | 下界 | Nether | `task` | `-` | 记得带夏装 | - |
| 2 | `nether/distract_piglin` | 金光闪闪！ | Oh Shiny | `task` | `nether/root` | 用金质物品让猪灵分神 | - |
| 3 | `nether/fast_travel` | 曲速泡 | Subspace Bubble | `challenge` | `nether/root` | 利用下界移动对应主世界7千米的距离 | +100XP |
| 4 | `nether/find_bastion` | 光辉岁月 | Those Were the Days | `task` | `nether/root` | 进入堡垒遗迹 | - |
| 5 | `nether/find_fortress` | 阴森的要塞 | A Terrible Fortress | `task` | `nether/root` | 闯入下界要塞 | - |
| 6 | `nether/obtain_ancient_debris` | 深藏不露 | Hidden in the Depths | `task` | `nether/root` | 获得远古残骸 | - |
| 7 | `nether/obtain_crying_obsidian` | 谁在切洋葱？ | Who is Cutting Onions? | `task` | `nether/root` | 获得哭泣的黑曜石 | - |
| 8 | `nether/return_to_sender` | 见鬼去吧 | Return to Sender | `challenge` | `nether/root` | 用一团火球干掉一只恶魂 | +50XP |
| 9 | `nether/ride_strider` | 画船添足 | This Boat Has Legs | `task` | `nether/root` | 手持诡异菌钓竿骑乘炽足兽 | - |
| 10 | `nether/charge_respawn_anchor` | 锚没有九条命 | Not Quite "Nine" Lives | `task` | `nether/obtain_crying_obsidian` | 为重生锚充满能量 | - |
| 11 | `nether/explore_nether` | 热门景点 | Hot Tourist Destinations | `challenge` | `nether/ride_strider` | 探索所有下界生物群系 | +500XP, 5项全满 |
| 12 | `nether/get_wither_skull` | 惊悚恐怖骷髅头 | Spooky Scary Skeleton | `task` | `nether/find_fortress` | 获得凋灵骷髅的头颅 | - |
| 13 | `nether/loot_bastion` | 战猪 | War Pigs | `task` | `nether/find_bastion` | 掠夺堡垒遗迹里的箱子 | - |
| 14 | `nether/netherite_armor` | 残骸裹身 | Cover Me in Debris | `challenge` | `nether/obtain_ancient_debris` | 获得一整套下界合金盔甲 | +100XP |
| 15 | `nether/obtain_blaze_rod` | 与火共舞 | Into Fire | `task` | `nether/find_fortress` | 让烈焰人从烈焰棒中解放吧 | - |
| 16 | `nether/ride_strider_in_overworld_lava` | 温暖如家 | Feels Like Home | `task` | `nether/ride_strider` | 带炽足兽在主世界的熔岩湖上来一场长——途旅行 | - |
| 17 | `nether/uneasy_alliance` | 脆弱的同盟 | Uneasy Alliance | `challenge` | `nether/return_to_sender` | 从下界救出一只恶魂，将其安全地带到主世界……然后干掉它 | +100XP |
| 18 | `nether/brew_potion` | 本地酿造厂 | Local Brewery | `task` | `nether/obtain_blaze_rod` | 酿造一瓶药水 | - |
| 19 | `nether/summon_wither` | 凋零山庄 | Withering Heights | `task` | `nether/get_wither_skull` | 召唤凋灵 | - |
| 20 | `nether/all_potions` | 狂乱的鸡尾酒 | A Furious Cocktail | `challenge` | `nether/brew_potion` | 同时拥有所有药水效果 | +100XP |
| 21 | `nether/create_beacon` | 带信标回家 | Bring Home the Beacon | `task` | `nether/summon_wither` | 建造并放置一个信标 | - |
| 22 | `nether/all_effects` | 为什么会变成这样呢？ | How Did We Get Here? | `challenge` | `nether/all_potions` | 同时拥有所有状态效果 | 隐藏, +1000XP |
| 23 | `nether/create_full_beacon` | 信标工程师 | Beaconator | `goal` | `nether/create_beacon` | 让一座信标发挥最大功效 | - |

## The End（末地）（共 9 项）

| # | ID (`minecraft:...`) | 中文名 | 英文名 | 级别 (`frame`) | 前置 (`parent`) | 达成条件（官方描述） | 备注 |
|---:|---|---|---|---|---|---|---|
| 1 | `end/root` | 末地 | The End | `task` | `-` | 抑或是起点？ | - |
| 2 | `end/kill_dragon` | 解放末地 | Free the End | `task` | `end/root` | 祝君好运 | - |
| 3 | `end/dragon_breath` | 你需要来点薄荷糖 | You Need a Mint | `goal` | `end/kill_dragon` | 用玻璃瓶收集一些龙息 | - |
| 4 | `end/dragon_egg` | 下一世代 | The Next Generation | `goal` | `end/kill_dragon` | 获得龙蛋 | - |
| 5 | `end/enter_end_gateway` | 远程折跃 | Remote Getaway | `task` | `end/kill_dragon` | 逃离这座岛屿 | - |
| 6 | `end/respawn_dragon` | 结束了…再一次… | The End... Again... | `goal` | `end/kill_dragon` | 复活末影龙 | - |
| 7 | `end/find_end_city` | 在游戏尽头的城市 | The City at the End of the Game | `task` | `end/enter_end_gateway` | 进去吧，又能发生什么呢？ | - |
| 8 | `end/elytra` | 天空即为极限 | Sky's the Limit | `goal` | `end/find_end_city` | 找到鞘翅 | - |
| 9 | `end/levitate` | 这上面的风景不错 | Great View From Up Here | `challenge` | `end/find_end_city` | 利用潜影贝的攻击向上飘浮50个方块 | +50XP |

## Adventure（冒险）（共 47 项）

| # | ID (`minecraft:...`) | 中文名 | 英文名 | 级别 (`frame`) | 前置 (`parent`) | 达成条件（官方描述） | 备注 |
|---:|---|---|---|---|---|---|---|
| 1 | `adventure/root` | 冒险 | Adventure | `task` | `-` | 冒险、探索与战斗 | - |
| 2 | `adventure/avoid_vibration` | 潜行100级 | Sneak 100 | `task` | `adventure/root` | 在幽匿感测体或监守者周围潜行以防被它们探测到 | - |
| 3 | `adventure/brush_armadillo` | 这不是鳞甲么？ | Isn't It Scute? | `task` | `adventure/root` | 用刷子从犰狳身上获得犰狳鳞甲 | - |
| 4 | `adventure/crafters_crafting_crafters` | 合成器合成合成器 | Crafters Crafting Crafters | `task` | `adventure/root` | 靠近一个正在合成合成器的合成器 | - |
| 5 | `adventure/fall_from_world_height` | 上天入地 | Caves & Cliffs | `task` | `adventure/root` | 从世界顶部（建筑高度限制处）自由落体，坠至世界底部并存活下来 | - |
| 6 | `adventure/heart_transplanter` | 移心接木 | Heart Transplanter | `task` | `adventure/root` | 将嘎枝之心以正确朝向放在两个苍白橡木原木方块中间 | - |
| 7 | `adventure/honey_block_slide` | 胶着状态 | Sticky Situation | `task` | `adventure/root` | 跳入蜂蜜块以缓冲摔落 | - |
| 8 | `adventure/kill_a_mob` | 怪物猎人 | Monster Hunter | `task` | `adventure/root` | 杀死任意敌对性怪物 | - |
| 9 | `adventure/lightning_rod_with_villager_no_fire` | 电涌保护器 | Surge Protector | `task` | `adventure/root` | 在不引发火灾的前提下保护村民免受雷击 | - |
| 10 | `adventure/minecraft_trials_edition` | Minecraft：试炼版 | Minecraft: Trial(s) Edition | `task` | `adventure/root` | 踏入试炼密室 | - |
| 11 | `adventure/ol_betsy` | 扣下悬刀 | Ol' Betsy | `task` | `adventure/root` | 用弩进行一次射击 | - |
| 12 | `adventure/read_power_of_chiseled_bookshelf` | 知识就是力量 | The Power of Books | `task` | `adventure/root` | 使用红石比较器获取雕纹书架的信号强度 | - |
| 13 | `adventure/salvage_sherd` | 探古寻源 | Respecting the Remnants | `task` | `adventure/root` | 刷扫可疑的方块来获得陶片 | 2项全满 |
| 14 | `adventure/sleep_in_bed` | 甜蜜的梦 | Sweet Dreams | `task` | `adventure/root` | 在床上睡觉以改变你的重生点 | - |
| 15 | `adventure/spyglass_at_parrot` | 那是鸟吗？ | Is It a Bird? | `task` | `adventure/root` | 透过望远镜观察鹦鹉 | - |
| 16 | `adventure/trade` | 成交！ | What a Deal! | `task` | `adventure/root` | 成功与一名村民进行交易 | - |
| 17 | `adventure/trim_with_any_armor_pattern` | 旧貌锻新颜 | Crafting a New Look | `task` | `adventure/root` | 在锻造台中合成带有纹饰的盔甲 | - |
| 18 | `adventure/use_lodestone` | 天涯共此石 | Country Lode, Take Me Home | `task` | `adventure/root` | 对着磁石使用指南针 | - |
| 19 | `adventure/voluntary_exile` | 自我放逐 | Voluntary Exile | `task` | `adventure/root` | 杀死一名袭击队长。 或许该考虑暂时远离村庄…… | 隐藏 |
| 20 | `adventure/adventuring_time` | 探索的时光 | Adventuring Time | `challenge` | `adventure/sleep_in_bed` | 发现所有的生物群系 | +500XP, 54项全满 |
| 21 | `adventure/arbalistic` | 劲弩手 | Arbalistic | `challenge` | `adventure/ol_betsy` | 用弩一发击杀五种生物 | 隐藏, +85XP |
| 22 | `adventure/blowback` | 逆风翻盘 | Blowback | `challenge` | `adventure/minecraft_trials_edition` | 反弹旋风人的风弹来击杀旋风人 | +40XP |
| 23 | `adventure/craft_decorated_pot_using_only_sherds` | 精修细补 | Careful Restoration | `task` | `adventure/salvage_sherd` | 用4个陶片制作饰纹陶罐 | - |
| 24 | `adventure/hero_of_the_village` | 村庄英雄 | Hero of the Village | `challenge` | `adventure/voluntary_exile` | 成功在袭击中保卫村庄 | 隐藏, +100XP |
| 25 | `adventure/kill_all_mobs` | 资深怪物猎人 | Monsters Hunted | `challenge` | `adventure/kill_a_mob` | 杀死每一种敌对性怪物 | +100XP, 41项全满 |
| 26 | `adventure/kill_mob_near_sculk_catalyst` | 它蔓延了 | It Spreads | `challenge` | `adventure/kill_a_mob` | 在幽匿催发体附近杀死生物 | - |
| 27 | `adventure/lighten_up` | 铜光焕发 | Lighten Up | `task` | `adventure/minecraft_trials_edition` | 用斧刮削铜灯来让它变得更亮 | - |
| 28 | `adventure/overoverkill` | 天赐良击 | Over-Overkill | `challenge` | `adventure/minecraft_trials_edition` | 使用重锤一击造成50颗心的伤害 | +50XP |
| 29 | `adventure/play_jukebox_in_meadows` | 音乐之声 | Sound of Music | `task` | `adventure/sleep_in_bed` | 用唱片机的音乐声为草甸增添生机 | - |
| 30 | `adventure/shoot_arrow` | 瞄准目标 | Take Aim | `task` | `adventure/kill_a_mob` | 用弓箭射点什么 | - |
| 31 | `adventure/spear_many_mobs` | 生物串串香 | Mob Kabob | `goal` | `adventure/kill_a_mob` | 用矛在一次冲锋攻击内命中五只生物 | - |
| 32 | `adventure/spyglass_at_ghast` | 那是气球吗？ | Is It a Balloon? | `task` | `adventure/spyglass_at_parrot` | 透过望远镜观察恶魂 | - |
| 33 | `adventure/summon_iron_golem` | 招募援兵 | Hired Help | `goal` | `adventure/trade` | 召唤一只铁傀儡来帮忙守卫村庄 | - |
| 34 | `adventure/throw_trident` | 抖包袱 | A Throwaway Joke | `task` | `adventure/kill_a_mob` | 往什么东西扔出三叉戟。 注：别把你唯一的武器也抖掉了。 | - |
| 35 | `adventure/totem_of_undying` | 超越生死 | Postmortal | `goal` | `adventure/kill_a_mob` | 利用不死图腾逃离死神 | - |
| 36 | `adventure/trade_at_world_height` | 星际商人 | Star Trader | `task` | `adventure/trade` | 在建筑高度限制处与村民交易 | - |
| 37 | `adventure/trim_with_all_exclusive_armor_patterns` | 匠心独具 | Smithing with Style | `challenge` | `adventure/trim_with_any_armor_pattern` | 将下列锻造模板都至少使用一次：尖塔、猪鼻、肋骨、监守、幽静、恼鬼、潮汐、向导 | +150XP, 8项全满 |
| 38 | `adventure/two_birds_one_arrow` | 一箭双雕 | Two Birds, One Arrow | `challenge` | `adventure/ol_betsy` | 用一支穿透箭射杀两只幻翼 | +65XP |
| 39 | `adventure/under_lock_and_key` | 珍藏密敛 | Under Lock and Key | `task` | `adventure/minecraft_trials_edition` | 用试炼钥匙解锁宝库 | - |
| 40 | `adventure/walk_on_powder_snow_with_leather_boots` | 轻功雪上飘 | Light as a Rabbit | `task` | `adventure/sleep_in_bed` | 在细雪上行走……并且不陷进去 | - |
| 41 | `adventure/who_needs_rockets` | 还要啥火箭啊？ | Who Needs Rockets? | `task` | `adventure/minecraft_trials_edition` | 使用风弹将自己向上弹射8格 | - |
| 42 | `adventure/whos_the_pillager_now` | 现在谁才是掠夺者？ | Who's the Pillager Now? | `task` | `adventure/ol_betsy` | 让掠夺者也尝尝弩的滋味 | - |
| 43 | `adventure/bullseye` | 正中靶心 | Bullseye | `challenge` | `adventure/shoot_arrow` | 从至少30米外射中标靶的靶心 | +50XP |
| 44 | `adventure/revaulting` | 宝经磨炼 | Revaulting | `goal` | `adventure/under_lock_and_key` | 用不祥试炼钥匙解锁不祥宝库 | - |
| 45 | `adventure/sniper_duel` | 狙击手的对决 | Sniper Duel | `challenge` | `adventure/shoot_arrow` | 从至少50米外射杀一只骷髅 | +50XP |
| 46 | `adventure/spyglass_at_dragon` | 那是飞机吗？ | Is It a Plane? | `task` | `adventure/spyglass_at_ghast` | 透过望远镜观察末影龙 | - |
| 47 | `adventure/very_very_frightening` | 魔女审判 | Very Very Frightening | `task` | `adventure/throw_trident` | 雷击一名村民 | - |

## Husbandry（农牧业）（共 30 项）

| # | ID (`minecraft:...`) | 中文名 | 英文名 | 级别 (`frame`) | 前置 (`parent`) | 达成条件（官方描述） | 备注 |
|---:|---|---|---|---|---|---|---|
| 1 | `husbandry/root` | 农牧业 | Husbandry | `task` | `-` | 这个世界充满朋友与美食 | - |
| 2 | `husbandry/allay_deliver_item_to_player` | 找到一个好朋友 | You've Got a Friend in Me | `task` | `husbandry/root` | 让悦灵向你投掷物品 | 隐藏 |
| 3 | `husbandry/breed_an_animal` | 我从哪儿来？ | The Parrots and the Bats | `task` | `husbandry/root` | 繁殖一对动物 | - |
| 4 | `husbandry/fishy_business` | 腥味十足的生意 | Fishy Business | `task` | `husbandry/root` | 钓到一条鱼 | - |
| 5 | `husbandry/make_a_sign_glow` | 眼前一亮！ | Glow and Behold! | `task` | `husbandry/root` | 让任意种类告示牌上的文本发光 | - |
| 6 | `husbandry/obtain_sniffer_egg` | 怪味蛋 | Smells Interesting | `task` | `husbandry/root` | 获得嗅探兽蛋 | 隐藏 |
| 7 | `husbandry/place_dried_ghast_in_water` | 补水保湿！ | Stay Hydrated! | `task` | `husbandry/root` | 将失水恶魂方块放在水中 | - |
| 8 | `husbandry/plant_seed` | 开荒垦地 | A Seedy Place | `task` | `husbandry/root` | 种下种子，见证它的成长 | - |
| 9 | `husbandry/ride_a_boat_with_a_goat` | 羊帆起航！ | Whatever Floats Your Goat! | `task` | `husbandry/root` | 与山羊同船共渡 | - |
| 10 | `husbandry/safely_harvest_honey` | 与蜂共舞 | Bee Our Guest | `task` | `husbandry/root` | 利用营火，在不惊动蜜蜂的情况下，用玻璃瓶从蜂巢收集蜂蜜 | - |
| 11 | `husbandry/silk_touch_nest` | 举巢搬迁 | Total Beelocation | `task` | `husbandry/root` | 用精准采集移动住着3只蜜蜂的蜂巢或蜂箱 | - |
| 12 | `husbandry/tadpole_in_a_bucket` | 蚪到桶里来 | Bukkit Bukkit | `task` | `husbandry/root` | 用铁桶捕获一只蝌蚪 | - |
| 13 | `husbandry/tame_an_animal` | 永恒的伙伴 | Best Friends Forever | `task` | `husbandry/root` | 驯服一只动物 | - |
| 14 | `husbandry/allay_deliver_cake_to_note_block` | 生日快乐歌 | Birthday Song | `task` | `husbandry/allay_deliver_item_to_player` | 让悦灵向音符盒投掷一块蛋糕 | 隐藏 |
| 15 | `husbandry/balanced_diet` | 均衡饮食 | A Balanced Diet | `challenge` | `husbandry/plant_seed` | 尝遍天下食材，即便是对身体不好的 | +100XP, 40项全满 |
| 16 | `husbandry/bred_all_animals` | 成双成对 | Two by Two | `challenge` | `husbandry/breed_an_animal` | 繁殖每种动物！ | +100XP, 26项全满 |
| 17 | `husbandry/complete_catalogue` | 百猫全书 | A Complete Catalogue | `challenge` | `husbandry/tame_an_animal` | 驯服所有种类的猫！ | +50XP, 11项全满 |
| 18 | `husbandry/feed_snifflet` | 小小嗅探兽 | Little Sniffs | `task` | `husbandry/obtain_sniffer_egg` | 喂食一只小嗅探兽 | 隐藏 |
| 19 | `husbandry/leash_all_frog_variants` | 呱呱队出动 | When the Squad Hops into Town | `task` | `husbandry/tadpole_in_a_bucket` | 用拴绳拴住所有种类的青蛙 | 3项全满 |
| 20 | `husbandry/obtain_netherite_hoe` | 终极奉献 | Serious Dedication | `challenge` | `husbandry/plant_seed` | 用下界合金锭升级一把锄，然后重新考虑你的人生抉择 | +100XP |
| 21 | `husbandry/remove_wolf_armor` | 华丽一剪 | Shear Brilliance | `task` | `husbandry/tame_an_animal` | 用剪刀移除狼身上的狼铠 | - |
| 22 | `husbandry/repair_wolf_armor` | 完好如初 | Good as New | `task` | `husbandry/tame_an_animal` | 用犰狳鳞甲修复损坏的狼铠 | - |
| 23 | `husbandry/tactical_fishing` | 战术性钓鱼 | Tactical Fishing | `task` | `husbandry/fishy_business` | 不用钓鱼竿抓住一条鱼！ | - |
| 24 | `husbandry/wax_on` | 涂蜡 | Wax On | `task` | `husbandry/safely_harvest_honey` | 将蜜脾涂到铜质方块上！ | - |
| 25 | `husbandry/whole_pack` | 群狼聚首 | The Whole Pack | `challenge` | `husbandry/tame_an_animal` | 驯服所有种类的狼 | +50XP, 9项全满 |
| 26 | `husbandry/axolotl_in_a_bucket` | 最萌捕食者 | The Cutest Predator | `task` | `husbandry/tactical_fishing` | 用铁桶捕获一只美西螈 | - |
| 27 | `husbandry/froglights` | 相映生辉！ | With Our Powers Combined! | `challenge` | `husbandry/leash_all_frog_variants` | 在你的物品栏中集齐所有种类的蛙明灯 | - |
| 28 | `husbandry/plant_any_sniffer_seed` | 播种往事 | Planting the Past | `task` | `husbandry/feed_snifflet` | 种植任意嗅探兽种子 | 隐藏 |
| 29 | `husbandry/wax_off` | 脱蜡 | Wax Off | `task` | `husbandry/wax_on` | 给铜质方块脱蜡！ | - |
| 30 | `husbandry/kill_axolotl_target` | 友谊的治愈力！ | The Healing Power of Friendship! | `task` | `husbandry/axolotl_in_a_bucket` | 与美西螈并肩作战并赢得胜利 | - |
