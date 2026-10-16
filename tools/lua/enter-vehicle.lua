-- test instance only: put the local character in the driver seat of the first RV
local v = FindFirstOf("BP_Vehicle_Winnebago_01_C")
local pc = FindFirstOf("PlayerController")
if not (v and v:IsValid()) then print("no vehicle (level not loaded yet)") return end
if pc.Pawn:GetFullName():find("Winnebago") then print("already driving") return end
v:TryToEnterVehicle(pc.Pawn)
print("entered; pawn =", pc.Pawn:GetFullName())
