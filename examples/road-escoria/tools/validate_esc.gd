extends Node
# Compiles every ESC script in the project and quits. Errors show up in the log
# as "Error(s) found during parsing of script ...".

func _ready() -> void:
	var files: Array[String] = []
	_collect("res://", files)
	for path in files:
		escoria.esc_compiler.load_esc_file(path)
	print("validate_esc: checked %d scripts" % files.size())
	get_tree().quit()


func _collect(dir_path: String, files: Array[String]) -> void:
	for d in DirAccess.get_directories_at(dir_path):
		if not d.begins_with(".") and d != "addons" and d != "build":
			_collect(dir_path.path_join(d), files)
	for f in DirAccess.get_files_at(dir_path):
		if f.ends_with(".esc"):
			files.append(dir_path.path_join(f))
