-- test instance only: load the jungle level as a standalone game
local UEH = require("UEHelpers")
UEH.GetKismetSystemLibrary():ExecuteConsoleCommand(UEH.GetWorld(),
    "open /Game/Ride/Maps/JungleLevel?game=/Game/Ride/Character/Blueprints/GM_FirstPerson.GM_FirstPerson_C",
    FindFirstOf("PlayerController"))
print("sent open")
