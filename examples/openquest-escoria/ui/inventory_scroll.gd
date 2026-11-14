extends Button
# Inventory scroll arrow (SCUMM's invUp / invDown verbs).

@export var step: int = 0


func _ready() -> void:
	pressed.connect(func(): get_node("../ScrollContainer").scroll_vertical += step)
